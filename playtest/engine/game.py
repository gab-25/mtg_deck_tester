"""The game loop: turns, steps, priority, and asking agents what to do.

Each step runs its turn-based actions, then a priority window when the step
has one. Inside a window, state-based actions run at the top of every
iteration (so a player who is already dead is never asked anything), pending
triggers go on the stack in APNAP order, and the stack drains *iteratively*
— a deep stack never recurses. When ``Pass`` is the only legal action, the
engine passes without asking: that is the vast majority of windows.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable

from .actions import Action, ActionKind, can_attack, dead_spells, legal_actions
from .dsl import Trigger
from .effects import draw_cards, target_choices, target_slots
from .events import Event, EventKind
from .limits import Limits
from .rules import (
    IllegalAction,
    apply,
    check_state,
    cleanup_step,
    combat_damage,
    empty_mana_pools,
    end_combat,
    has_first_strikers,
    london_mulligan,
    resolve_top,
    trigger_item,
    untap_step,
)
from .state import Card, Combat, GameRules, GameState, PendingChoice, PendingTarget, Player, Step, assert_invariants
from .view import player_view
from .zones import queue_event_triggers, queue_triggers

if TYPE_CHECKING:
    from ..agents.base import Agent
    from .cards import DeckSpec


class EndReason:
    LAST_STANDING = "last_standing"
    TURN_LIMIT = "turn_limit"


@dataclass
class RecordedEvent:
    """An :class:`Event` stamped with where it happened in the game."""

    seq: int
    round: int
    turn: int
    step: str
    event: Event


@dataclass
class SeatResult:
    seat: int
    life: int
    eliminated_round: int | None


@dataclass
class GameResult:
    winner_seat: int | None
    end_reason: str
    rounds: int
    seats: list[SeatResult]
    events: list[RecordedEvent] = field(default_factory=list)


class _GameOver(Exception):
    """Unwinds the loop as soon as one player (or none) is left."""


class _TurnOver(Exception):
    """Unwinds the turn when its active player leaves the game."""


class Game:
    """One game between ``len(decks)`` players, each driven by an agent.

    Deterministic for a given ``seed`` as long as the agents are: shuffles and
    the choice of the first player each use their own ``random.Random``
    derived from the seed. ``on_event`` is called with every
    :class:`RecordedEvent` as it happens, so a caller can persist or stream the
    game while it runs. ``clock`` (seconds, monotonic) is injectable for tests.
    """

    def __init__(
        self,
        decks: list[DeckSpec],
        agents: list[Agent],
        rules: GameRules,
        *,
        seed: int,
        max_rounds: int | None = None,
        limits: Limits | None = None,
        on_event: Callable[[RecordedEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_invariants: bool = False,
    ):
        if len(decks) != len(agents):
            raise ValueError("Every deck needs an agent.")
        if len(decks) < 2:
            raise ValueError("A game needs at least two players.")
        limits = limits or Limits()
        if max_rounds is not None:
            limits = replace(limits, max_rounds=max_rounds)
        self.decks = decks
        self.agents = list(agents)
        self.rules = rules
        self.limits = limits
        self.seed = seed
        # One generator per purpose, so adding a shuffle never moves the first player.
        self.shuffle_rng = random.Random(f"{seed}:shuffle")
        self.order_rng = random.Random(f"{seed}:order")
        self.on_event = on_event
        self.clock = clock
        self.check_invariants = check_invariants
        self.events: list[RecordedEvent] = []
        self.agent_calls = [0] * len(decks)
        self.decisions_by_seat = [0] * len(decks)
        self.decisions_this_turn = 0
        self.triggers_this_turn = 0
        self.forced_pass = False
        self.started_at: float | None = None
        self.clock_expired = False
        self.state = self._setup()
        self.cards_per_seat = {
            p.seat: len(p.library) + len(p.hand) + len(p.command) for p in self.state.players
        }

    # -- setup -------------------------------------------------------------

    def _setup(self) -> GameState:
        state = GameState(rules=self.rules, players=[], rng=random.Random(f"{self.seed}:effects"))
        for seat, deck in enumerate(self.decks):
            library = [Card(state.new_id(), spec, seat) for spec in deck.library]
            command = [Card(state.new_id(), spec, seat, is_commander=True) for spec in deck.commanders]
            for commander in command:
                state.commanders[commander.id] = (seat, commander.name)
            self.shuffle_rng.shuffle(library)
            state.players.append(
                Player(seat=seat, name=deck.name, life=self.rules.starting_life, library=library, command=command)
            )
        return state

    # -- bookkeeping -------------------------------------------------------

    def _record(self, events: list[Event]) -> None:
        state = self.state
        for event in events:
            recorded = RecordedEvent(len(self.events) + 1, state.round, state.turn, str(state.step), event)
            self.events.append(recorded)
            if self.on_event is not None:
                self.on_event(recorded)

    def _check(self) -> None:
        if self.check_invariants:
            assert_invariants(self.state, self.cards_per_seat)

    def _settle(self) -> None:
        """State-based actions; ends the game or the turn when they say so."""
        self._record(check_state(self.state, self.limits))
        self._check()
        if len(self.state.alive_players()) <= 1:
            raise _GameOver
        if not self.state.player(self.state.active_seat).alive:
            raise _TurnOver

    # -- the loop ----------------------------------------------------------

    def run(self) -> GameResult:
        state = self.state
        self.started_at = self.clock()
        seats = len(state.players)
        first = self.order_rng.randrange(seats)
        state.order = [(first + i) % seats for i in range(seats)]
        self._record(
            [
                Event(
                    EventKind.SETUP,
                    None,
                    f"{seats} players at {self.rules.starting_life} life. {state.player(first).name} goes first.",
                    {
                        "first_seat": first,
                        "decks": [p.name for p in state.players],
                        "starting_life": self.rules.starting_life,
                    },
                )
            ]
        )
        for seat in state.order:
            self._record(london_mulligan(state, seat, self.shuffle_rng, free_first=seats >= 3))
        self._check()

        try:
            for round_no in range(1, self.limits.max_rounds + 1):
                state.round = round_no
                for seat in state.order:
                    if state.player(seat).alive:
                        self._take_turn(seat)
        except _GameOver:
            return self._finish(EndReason.LAST_STANDING)
        return self._finish(EndReason.TURN_LIMIT)

    def _take_turn(self, seat: int) -> None:
        state = self.state
        state.turn += 1
        state.active_seat = seat
        self.decisions_this_turn = 0
        self.triggers_this_turn = 0
        self.forced_pass = False
        state.step = Step.UNTAP
        self._record([Event(EventKind.TURN, seat, f"Round {state.round}: {state.player(seat).name}'s turn.")])
        skip_combat = False
        dealt_first: set[int] = set()
        try:
            for step in Step:
                state.step = step
                if skip_combat and step in (
                    Step.DECLARE_ATTACKERS,
                    Step.DECLARE_BLOCKERS,
                    Step.FIRST_STRIKE_DAMAGE,
                    Step.COMBAT_DAMAGE,
                ):
                    continue
                match step:
                    case Step.UNTAP:
                        untap_step(state, seat)
                    case Step.UPKEEP:
                        self._queue_step_triggers(seat, "UPKEEP")
                        self._priority()
                    case Step.DRAW:
                        # CR 103.8a: in a two-player game the first player skips their first draw.
                        if not (state.turn == 1 and len(state.players) == 2):
                            self._record(draw_cards(state, seat, 1))
                        self._priority()
                    case Step.MAIN1 | Step.MAIN2:
                        self._priority()
                    case Step.BEGIN_COMBAT:
                        self._queue_step_triggers(seat, Trigger.BEGIN_COMBAT)
                        if not any(can_attack(state, p) for p in state.battlefield):
                            skip_combat = True
                            if state.triggers:
                                self._priority()
                            continue
                        self._priority()
                    case Step.DECLARE_ATTACKERS:
                        if not self._declare_attackers(seat):
                            skip_combat = True
                            continue
                        self._priority()
                    case Step.DECLARE_BLOCKERS:
                        self._declare_blockers(seat)
                        self._priority()
                    case Step.FIRST_STRIKE_DAMAGE:
                        if not has_first_strikers(state):
                            continue
                        self._record(combat_damage(state, True, dealt_first))
                        self._priority()
                    case Step.COMBAT_DAMAGE:
                        self._record(combat_damage(state, False, dealt_first))
                        self._priority()
                    case Step.END_COMBAT:
                        if not skip_combat:
                            self._priority()
                        end_combat(state)
                    case Step.END_STEP:
                        self._queue_step_triggers(seat, "END_STEP")
                        self._priority()
                    case Step.CLEANUP:
                        self._record(cleanup_step(state, seat))
                        self._settle()
                        if state.triggers:
                            # CR 514.3a: triggers during cleanup get a priority window.
                            self._priority()
                empty_mana_pools(state)
        except _TurnOver:
            pass
        finally:
            end_combat(state)
            state.pending_target = None
            empty_mana_pools(state)
            # Only when the active player left mid-turn: their objects are gone
            # already (CR 800.4a); everyone else's spells and triggers stay, and
            # are dealt with in the next turn's first priority window.

    def _queue_step_triggers(self, seat: int, trigger: str) -> None:
        for perm in sorted(self.state.controlled(seat), key=lambda p: p.id):
            queue_triggers(self.state, perm, trigger)

    # -- priority ----------------------------------------------------------

    def _next_seat(self, seat: int) -> int:
        """The next living seat after ``seat`` in turn order (``seat`` itself may be gone)."""
        state = self.state
        order = state.order
        start = order.index(seat)
        for offset in range(1, len(order) + 1):
            candidate = order[(start + offset) % len(order)]
            if state.player(candidate).alive:
                return candidate
        return seat

    def _priority(self) -> None:
        state = self.state
        seat = state.active_seat
        passes = 0
        while True:
            self._settle()
            if self._flush_triggers():
                passes = 0
                seat = state.active_seat
                continue
            if not state.player(seat).alive:
                seat = self._next_seat(seat)
            self._report_dead_spells(seat)
            action = self._decide(seat)
            if action.kind == ActionKind.PASS:
                passes += 1
                if passes >= len(state.alive_players()):
                    if not state.stack:
                        return
                    self._record(resolve_top(state, self._choose))
                    passes = 0
                    seat = state.active_seat
                    continue
                seat = self._next_seat(seat)
                continue
            self._record(apply(state, seat, action, self.limits))
            self._check()
            passes = 0
            self._choose_pending_targets()

    def _flush_triggers(self) -> bool:
        """Puts waiting triggers on the stack, APNAP order; True if any went on."""
        state = self.state
        if not state.triggers:
            return False
        waiting, state.triggers = state.triggers, []
        order = state.turn_order_from(state.active_seat)
        waiting.sort(key=lambda t: order.index(t.controller) if t.controller in order else len(order))
        for trigger in waiting:
            if not state.player(trigger.controller).alive:
                continue
            if self.triggers_this_turn >= self.limits.max_triggers_per_turn:
                self._record(
                    [Event(EventKind.LIMIT, trigger.controller, f"Too many triggers this turn: {trigger.source_name}'s trigger is dropped.", {"limit": "triggers"})]
                )
                continue
            self.triggers_this_turn += 1
            if len(state.stack) >= self.limits.max_stack_depth:
                self._record(
                    [Event(EventKind.LIMIT, trigger.controller, f"The stack is full: {trigger.source_name}'s trigger is dropped.", {"limit": "stack_depth"})]
                )
                continue
            item = trigger_item(state, trigger)
            slots = target_slots(item.abilities)
            if any(not target_choices(state, target, item.controller, item.id) for _, target in slots):
                self._record(
                    [Event(EventKind.TRIGGER, trigger.controller, f"{trigger.source_name}'s ability has no legal target.", {"card": trigger.source_name, "removed": True})]
                )
                continue
            state.stack.append(item)
            self._record([Event(EventKind.TRIGGER, trigger.controller, f"{trigger.source_name}'s ability triggers.", {"card": trigger.source_name})])
            if slots:
                state.pending_target = PendingTarget(item.id, slots[0][0])
                self._choose_pending_targets()
        return True

    def _choose_pending_targets(self) -> None:
        state = self.state
        while state.pending_target is not None:
            item = state.stack_item(state.pending_target.item_id)
            if item is None:
                state.pending_target = None
                return
            if not target_choices(state, dict(target_slots(item.abilities))[state.pending_target.key], item.controller, item.id):
                state.pending_target = None
                state.stack.remove(item)
                self._record([Event(EventKind.FIZZLE, item.controller, f"{item.name} has no legal target and is removed.", {"card": item.name})])
                return
            action = self._decide(item.controller)
            self._record(apply(state, item.controller, action, self.limits))
            self._check()

    def _choose(self, state: GameState, seat: int, kind: str, prompt: str, options) -> str:
        """Asks ``seat``'s agent to answer a choice an effect needs while it resolves."""
        state.pending_choice = PendingChoice(seat, kind, prompt, list(options))
        try:
            action = self._decide(seat)
        finally:
            state.pending_choice = None
        return action.ref

    def _report_dead_spells(self, seat: int) -> None:
        player = self.state.player(seat)
        for card in dead_spells(self.state, seat):
            if card.id not in player.reported_dead:
                player.reported_dead.add(card.id)
                self._record(
                    [
                        Event(
                            EventKind.UNSUPPORTED_SKIPPED,
                            seat,
                            f"{player.name} could cast {card.name}, but its effect is not supported: it stays in hand as a blank.",
                            {"card": card.name},
                        )
                    ]
                )

    # -- combat declarations ---------------------------------------------

    def _declare(self, seat: int, combat: Combat) -> None:
        state = self.state
        state.combat = combat
        while state.combat is not None:
            if combat.steps >= self.limits.max_declaration_steps:
                self._record(
                    [Event(EventKind.LIMIT, seat, f"{state.player(seat).name} hit the declaration limit.", {"limit": "declaration_steps"})]
                )
                self._force_end_declaration(seat)
                return
            action = self._decide(seat)
            self._record(apply(state, seat, action, self.limits))
            self._check()

    def _force_end_declaration(self, seat: int) -> None:
        state = self.state
        if state.combat.phase == "blockers":
            # Drop the lone blockers of menace attackers, which make the block illegal.
            for attacker in state.battlefield:
                if attacker.attacking == seat and len(attacker.blocked_by) == 1 and "menace" in attacker.spec.keywords:
                    blocker = state.permanent(attacker.blocked_by[0])
                    blocker.blocking = None
                    attacker.blocked_by.clear()
                    attacker.was_blocked = False
        state.combat = None

    def _declare_attackers(self, seat: int) -> bool:
        state = self.state
        self._declare(seat, Combat("attackers"))
        attackers = sorted((p for p in state.battlefield if p.attacking is not None), key=lambda p: p.id)
        for perm in attackers:
            queue_triggers(state, perm, "ATTACKS")
        if attackers:
            queue_event_triggers(state, Trigger.YOU_ATTACK, lambda perm, ability: perm.controller == seat)
        return bool(attackers)

    def _declare_blockers(self, seat: int) -> None:
        state = self.state
        for defender in state.turn_order_from(seat)[1:]:
            if any(p.attacking == defender for p in state.battlefield):
                self._declare(defender, Combat("blockers", defender))

    # -- asking agents -----------------------------------------------------

    def _decide(self, seat: int) -> Action:
        state = self.state
        options = legal_actions(state, seat, self.limits)
        if len(options) > self.limits.max_options_per_decision:
            self._record(
                [Event(EventKind.LIMIT, seat, f"{len(options)} options trimmed to {self.limits.max_options_per_decision}.", {"limit": "options", "options": len(options)})]
            )
            options = options[: self.limits.max_options_per_decision]
        nothing = options[0] if options[0].is_nothing else None
        if len(options) == 1:
            # Nothing to decide: don't spend an agent call on a forced choice.
            return options[0]
        if self.forced_pass:
            return nothing or options[0]
        if self.decisions_this_turn >= self.limits.max_decisions_per_turn:
            self.forced_pass = True
            self._record(
                [
                    Event(
                        EventKind.DECISION_LIMIT,
                        seat,
                        f"The limit of {self.limits.max_decisions_per_turn} decisions this turn was reached: "
                        "everyone passes until the turn ends.",
                    )
                ]
            )
            return nothing or options[0]
        self.decisions_this_turn += 1
        self.decisions_by_seat[seat] += 1
        self._meter(seat)
        agent = self.agents[seat]
        decision = agent.choose(player_view(state, seat), options)
        if getattr(agent, "metered", False):
            self.agent_calls[seat] += 1
        if decision.action not in options:
            raise IllegalAction(f"Agent chose an action that was not offered: {decision.action}")
        payload = {"action": decision.action.to_dict(), **decision.payload()}
        name = state.player(seat).name
        if decision.fallback_reason:
            self._record(
                [
                    Event(
                        EventKind.AGENT_FALLBACK,
                        seat,
                        f"{name}'s agent fell back to {decision.fallback_agent or 'another agent'} "
                        f"({decision.fallback_reason}), which chooses: {decision.action.label}.",
                        payload,
                    )
                ]
            )
        elif decision.reasoning or decision.confidence is not None:
            self._record(
                [
                    Event(
                        EventKind.PASS if decision.action.kind == ActionKind.PASS else EventKind.DECISION,
                        seat,
                        f"{name} chooses: {decision.action.label}.",
                        payload,
                        reasoning=decision.reasoning,
                    )
                ]
            )
        return decision.action

    def _meter(self, seat: int) -> None:
        """Hands metered (model-backed) seats to their fallback past the call or time budget."""
        agent = self.agents[seat]
        if not getattr(agent, "metered", False):
            return
        reason = ""
        if self.agent_calls[seat] >= self.limits.max_agent_calls:
            reason = f"{self.limits.max_agent_calls} agent calls"
        elif self.started_at is not None and self.clock() - self.started_at >= self.limits.max_wall_clock_seconds:
            reason = f"the {self.limits.max_wall_clock_seconds}s match time budget"
        if not reason:
            return
        targets = [seat]
        if reason.startswith("the"):
            # The clock is shared: every metered seat is handed over at once.
            targets = [s for s, a in enumerate(self.agents) if getattr(a, "metered", False)]
        for target in targets:
            fallback = self.agents[target].fallback
            self.agents[target] = fallback
            self._record(
                [
                    Event(
                        EventKind.WARNING,
                        target,
                        f"{self.state.player(target).name}'s agent reached {reason}: "
                        "the heuristic agent plays the rest of the game.",
                        {"reason": "budget", "budget": reason},
                    )
                ]
            )

    # -- the end -----------------------------------------------------------

    def _finish(self, reason: str) -> GameResult:
        state = self.state
        alive = state.alive_players()
        winner = alive[0].seat if reason == EndReason.LAST_STANDING and len(alive) == 1 else None
        if winner is not None:
            text = f"{state.player(winner).name} wins in round {state.round}."
        elif reason == EndReason.TURN_LIMIT:
            text = f"Round limit ({self.limits.max_rounds}) reached: the game is a draw."
        else:
            text = "Nobody is left standing: the game is a draw."
        seats = [
            {
                "seat": p.seat,
                "life": p.life,
                "decisions": self.decisions_by_seat[p.seat],
                "agent_calls": self.agent_calls[p.seat],
                "dead_spells_drawn": p.dead_drawn,
            }
            for p in state.players
        ]
        self._record(
            [Event(EventKind.GAME_OVER, winner, text, {"winner_seat": winner, "reason": reason, "seats": seats})]
        )
        return GameResult(
            winner_seat=winner,
            end_reason=reason,
            rounds=state.round,
            seats=[SeatResult(p.seat, p.life, p.eliminated_round) for p in state.players],
            events=self.events,
        )
