"""The game loop: turns, steps, priority, and asking agents what to do."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .actions import CLOSING_KINDS, Action, ActionKind, legal_actions
from .cards import DeckSpec
from .events import Event, EventKind
from .limits import Limits
from .rules import (
    IllegalAction,
    apply,
    check_state_based_actions,
    cleanup,
    combat_damage,
    draw,
    end_combat,
    end_turn_early,
    force_end_declaration,
    has_first_strike_step,
    opening_hand,
    resolve_top,
    untap_step,
)
from .state import (
    Card,
    Declaring,
    GameRules,
    GameState,
    PlayerState,
    Step,
    assert_invariants,
)
from .view import player_view

if TYPE_CHECKING:
    from ..agents.base import Agent


class EndReason:
    LAST_STANDING = "last_standing"
    TURN_LIMIT = "turn_limit"


@dataclass
class RecordedEvent:
    """An :class:`Event` stamped with where it happened in the game."""

    seq: int
    round: int
    turn: int
    event: Event
    # The step of the turn, empty before the first turn.
    step: str = ""


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


class _EndTurn(Exception):
    """The turn ends now: its player lost, or it ran out of decisions."""


class _GameOver(Exception):
    """At most one player is left."""


class Game:
    """One game between ``len(decks)`` players, each driven by an agent.

    Deterministic for a given ``seed`` as long as the agents are: each purpose
    (shuffling, choosing the first player) draws from its own ``random.Random``
    derived from the seed. ``on_event`` is called with every
    :class:`RecordedEvent` as it happens, so a caller can persist or stream the
    game while it runs. ``check_invariants`` asserts the state's invariants
    after every action (tests turn it on).
    """

    def __init__(
        self,
        decks: list[DeckSpec],
        agents: list[Agent],
        rules: GameRules,
        *,
        seed: int,
        max_rounds: int | None = None,
        max_decisions_per_turn: int | None = None,
        limits: Limits | None = None,
        on_event: Callable[[RecordedEvent], None] | None = None,
        check_invariants: bool = False,
    ):
        if len(decks) != len(agents):
            raise ValueError("Every deck needs an agent.")
        if len(decks) < 2:
            raise ValueError("A game needs at least two players.")
        limits = limits or Limits()
        overrides = {}
        if max_rounds is not None:
            overrides["max_rounds"] = max_rounds
        if max_decisions_per_turn is not None:
            overrides["max_decisions_per_turn"] = max_decisions_per_turn
        self.limits = replace(limits, **overrides)
        self.decks = decks
        self.agents = agents
        self.rules = rules
        self.shuffle_rng = random.Random(f"{seed}:shuffle")
        self.first_player_rng = random.Random(f"{seed}:first_player")
        self.on_event = on_event
        self.check_invariants = check_invariants
        self.events: list[RecordedEvent] = []
        self.decisions = 0
        self.state = self._setup()

    @property
    def max_rounds(self) -> int:
        return self.limits.max_rounds

    # -- setup -------------------------------------------------------------

    def _setup(self) -> GameState:
        state = GameState(rules=self.rules, players=[], limits=self.limits)
        for seat, deck in enumerate(self.decks):
            library = [Card(state.new_id(), spec, seat) for spec in deck.library]
            self.shuffle_rng.shuffle(library)
            commander = Card(state.new_id(), deck.commander, seat, is_commander=True)
            state.commanders[commander.id] = seat
            state.players.append(
                PlayerState(
                    seat=seat,
                    deck_name=deck.name,
                    life=self.rules.starting_life,
                    library=library,
                    command=[commander],
                )
            )
            state.total_cards += len(library) + 1
        return state

    # -- bookkeeping -------------------------------------------------------

    def _record(self, events: list[Event]) -> None:
        state = self.state
        for event in events:
            recorded = RecordedEvent(
                len(self.events) + 1, state.round, state.turn, event, str(state.step) if state.step else ""
            )
            self.events.append(recorded)
            if self.on_event is not None:
                self.on_event(recorded)

    def _limit(self, seat: int | None, name: str, text: str) -> None:
        self._record([Event(EventKind.LIMIT, seat, text, {"limit": name, "value": getattr(self.limits, name)})])

    def _check(self) -> None:
        if self.check_invariants:
            assert_invariants(self.state)

    # -- the loop ----------------------------------------------------------

    def run(self) -> GameResult:
        state = self.state
        seats = len(state.players)
        first = self.first_player_rng.randrange(seats)
        self._record(
            [
                Event(
                    EventKind.SETUP,
                    None,
                    f"{seats} players at {self.rules.starting_life} life. "
                    f"{state.player(first).deck_name} goes first.",
                    {
                        "first_seat": first,
                        "decks": [p.deck_name for p in state.players],
                        "starting_life": self.rules.starting_life,
                    },
                )
            ]
        )
        for seat in state.seats_from(first):
            # CR 103.5c: the first mulligan is free with three or more players.
            self._record(opening_hand(state, state.player(seat), self.shuffle_rng, seats >= 3))
        self._check()

        order = state.seats_from(first)
        try:
            for round_no in range(1, self.max_rounds + 1):
                state.round = round_no
                for seat in order:
                    if state.player(seat).alive:
                        self._take_turn(seat)
        except _GameOver:
            return self._finish(EndReason.LAST_STANDING)
        return self._finish(EndReason.TURN_LIMIT)

    def _take_turn(self, seat: int) -> None:
        state = self.state
        state.turn += 1
        state.active_seat = seat
        state.step = None
        self.decisions = 0
        self._record(
            [Event(EventKind.TURN, seat, f"Round {state.round}: {state.player(seat).deck_name}'s turn.")]
        )
        try:
            for step in Step:
                state.step = step
                self._STEPS[step](self)
        except _EndTurn:
            self._record(end_turn_early(state))
            state.step = Step.CLEANUP
            self._record(cleanup(state))
            self._check()
            self._settle()

    # -- steps -------------------------------------------------------------

    def _untap(self) -> None:
        untap_step(self.state)

    def _draw(self) -> None:
        state = self.state
        # CR 103.8a: in a two-player game the first player skips their first draw.
        if not (state.turn == 1 and len(state.players) == 2):
            self._record(draw(state, state.active_seat))
        self._priority()

    def _declare_attackers(self) -> None:
        state = self.state
        self._declare(Declaring.ATTACKERS, state.active_seat)
        if state.attackers():
            self._priority()

    def _declare_blockers(self) -> None:
        state = self.state
        if not state.attackers():
            return
        for seat in state.seats_from(state.active_seat)[1:]:
            if any(a.attacking == seat for a in state.attackers()):
                self._declare(Declaring.BLOCKERS, seat)
        self._priority()

    def _first_strike_damage(self) -> None:
        state = self.state
        if state.attackers() and has_first_strike_step(state):
            self._record(combat_damage(state, Step.FIRST_STRIKE_DAMAGE))
            self._priority()

    def _combat_damage(self) -> None:
        state = self.state
        if state.attackers():
            self._record(combat_damage(state, Step.COMBAT_DAMAGE))
            self._priority()

    def _end_combat(self) -> None:
        self._priority()
        end_combat(self.state)

    def _cleanup(self) -> None:
        self._record(cleanup(self.state))
        self._settle()

    _STEPS: ClassVar[dict] = {
        Step.UNTAP: _untap,
        Step.UPKEEP: lambda self: self._priority(),
        Step.DRAW: _draw,
        Step.MAIN1: lambda self: self._priority(),
        Step.BEGIN_COMBAT: lambda self: self._priority(),
        Step.DECLARE_ATTACKERS: _declare_attackers,
        Step.DECLARE_BLOCKERS: _declare_blockers,
        Step.FIRST_STRIKE_DAMAGE: _first_strike_damage,
        Step.COMBAT_DAMAGE: _combat_damage,
        Step.END_COMBAT: _end_combat,
        Step.MAIN2: lambda self: self._priority(),
        Step.END_STEP: lambda self: self._priority(),
        Step.CLEANUP: _cleanup,
    }

    # -- priority and declarations -----------------------------------------

    def _settle(self) -> None:
        """State-based actions; ends the game or the turn when they say so."""
        state = self.state
        events, settled = check_state_based_actions(state)
        self._record(events)
        if not settled:
            self._limit(None, "max_sba_iterations", "State-based actions did not settle; moving on.")
        self._check()
        if len(state.alive_players()) <= 1:
            raise _GameOver
        if not state.player(state.active_seat).alive and state.step != Step.CLEANUP:
            raise _EndTurn

    def _priority(self) -> None:
        """One priority window: runs until every living player passes on an empty stack."""
        state = self.state
        holder = state.active_seat
        passes = 0
        while True:
            seated = len(state.alive_players())
            self._settle()
            if len(state.alive_players()) != seated:
                # Someone left: the passes so far no longer cover the table.
                passes = 0
            if not state.player(holder).alive:
                holder = state.seats_from(holder)[0]
            options = legal_actions(state, holder)
            if len(options) == 1:
                action = options[0]  # Only passing is legal: pass without asking.
            else:
                action = self._decide(holder, options)
            if action.kind != ActionKind.PASS:
                self._record(apply(state, holder, action))
                self._check()
                passes = 0
                continue  # The caster gets priority again.
            passes += 1
            living = state.seats_from(holder)
            if passes < len(living):
                holder = living[1 % len(living)]
                continue
            if not state.stack:
                return
            self._record(resolve_top(state))
            self._check()
            holder = state.active_seat
            passes = 0

    def _declare(self, kind: Declaring, seat: int) -> None:
        state = self.state
        state.declaring = kind
        state.declaring_seat = seat
        try:
            steps = 0
            while True:
                options = legal_actions(state, seat)
                if len(options) == 1 and options[0].kind in CLOSING_KINDS:
                    action = options[0]
                elif steps >= self.limits.max_declaration_steps:
                    self._limit(seat, "max_declaration_steps", f"{state.player(seat).deck_name} hit the "
                                f"limit of {self.limits.max_declaration_steps} declarations.")
                    self._record(force_end_declaration(state, seat))
                    return
                else:
                    steps += 1
                    action = self._decide(seat, options)
                self._record(apply(state, seat, action))
                self._check()
                if action.kind in CLOSING_KINDS:
                    return
        finally:
            state.declaring = None
            state.declaring_seat = None

    def _decide(self, seat: int, options: list[Action]) -> Action:
        state = self.state
        player = state.player(seat)
        if self.decisions >= self.limits.max_decisions_per_turn:
            self._record(
                [
                    Event(
                        EventKind.DECISION_LIMIT,
                        seat,
                        f"The limit of {self.limits.max_decisions_per_turn} decisions this turn was "
                        f"reached: {state.player(state.active_seat).deck_name}'s turn ends.",
                        {"limit": self.limits.max_decisions_per_turn},
                    )
                ]
            )
            raise _EndTurn
        self.decisions += 1
        cap = self.limits.max_options_per_decision
        if len(options) > cap:
            self._limit(seat, "max_options_per_decision", f"Only the first {cap} of {len(options)} options are offered.")
            options = options[:cap]

        decision = self.agents[seat].choose(player_view(state, seat), options)
        if decision.fallback_reason:
            self._record(
                [
                    Event(
                        EventKind.AGENT_FALLBACK,
                        seat,
                        f"{player.deck_name}'s agent fell back to another choice: {decision.fallback_reason}",
                    )
                ]
            )
        if decision.action not in options:
            raise IllegalAction(f"Agent chose an action that was not offered: {decision.action}")
        if decision.reasoning:
            self._record(
                [
                    Event(
                        EventKind.PASS if decision.action.kind == ActionKind.PASS else EventKind.DECISION,
                        seat,
                        f"{player.deck_name} chooses: {decision.action.label}.",
                        {"action": decision.action.to_dict()},
                        reasoning=decision.reasoning,
                    )
                ]
            )
        return decision.action

    # -- the end -----------------------------------------------------------

    def _finish(self, reason: str) -> GameResult:
        state = self.state
        alive = state.alive_players()
        winner = alive[0].seat if reason == EndReason.LAST_STANDING and len(alive) == 1 else None
        if winner is not None:
            text = f"{state.player(winner).deck_name} wins in round {state.round}."
        elif reason == EndReason.TURN_LIMIT:
            text = f"Round limit ({self.max_rounds}) reached: the game is a draw."
        else:
            text = "Nobody is left standing: the game is a draw."
        self._record([Event(EventKind.GAME_OVER, winner, text, {"winner_seat": winner, "reason": reason})])
        return GameResult(
            winner_seat=winner,
            end_reason=reason,
            rounds=state.round,
            seats=[SeatResult(p.seat, p.life, p.eliminated_round) for p in state.players],
            events=self.events,
        )
