"""The choices an agent is offered, and how they are listed.

The contract of :func:`legal_actions`, tested as such:

* ``actions[0]`` is ``Pass`` whenever the seat has priority; while it declares
  attackers or blockers, it is the action that ends the declaration (which
  menace can withhold, see below).
* The order is deterministic: sorted by ``(rank, card name, id, target)``,
  never by iterating a set.
* Every action is legal right now, and applying it never raises.
* It is pure: it never changes the state.

Combat is declared one creature at a time, so the options grow as a sum, not
as the ``2^n`` subsets of the creatures: one ``DeclareAttacker`` per eligible
creature and opponent, then ``EndAttackers``; each defending player then
declares one ``DeclareBlocker`` per blocker and attacker, can take a block
back, and ends with ``EndBlockers`` — withheld while a creature with menace is
blocked by exactly one creature.
"""

from dataclasses import dataclass, field

from .cards import Kind, cost_text
from .mana import available_mana, can_pay, mana_sources
from .state import MAIN_STEPS, Declaring, GameState, Permanent


class ActionKind:
    # Gives up priority; the step, or the top of the stack, moves on once every
    # living player passes in succession.
    PASS = "pass"
    PLAY_LAND = "play_land"
    CAST_SPELL = "cast_spell"
    CAST_COMMANDER = "cast_commander"
    DECLARE_ATTACKER = "declare_attacker"
    END_ATTACKERS = "end_attackers"
    DECLARE_BLOCKER = "declare_blocker"
    UNDO_BLOCKER = "undo_blocker"
    END_BLOCKERS = "end_blockers"
    # Chosen after a cast whose targets are too many to list with it (card effects, #28).
    CHOOSE_TARGET = "choose_target"


# The kinds that end a decision point without doing anything more.
CLOSING_KINDS = (ActionKind.PASS, ActionKind.END_ATTACKERS, ActionKind.END_BLOCKERS)

_RANK = {
    ActionKind.PASS: 0,
    ActionKind.END_ATTACKERS: 0,
    ActionKind.END_BLOCKERS: 0,
    ActionKind.PLAY_LAND: 1,
    ActionKind.CAST_COMMANDER: 2,
    ActionKind.CAST_SPELL: 3,
    ActionKind.DECLARE_ATTACKER: 4,
    ActionKind.DECLARE_BLOCKER: 5,
    ActionKind.UNDO_BLOCKER: 6,
    ActionKind.CHOOSE_TARGET: 7,
}

# The largest X offered for a spell with {X} in its cost.
MAX_X = 10


@dataclass(frozen=True)
class Action:
    kind: str
    # The card (in hand or in the command zone) or the permanent the action is about.
    card_uid: int | None = None
    # The player attacked.
    target_seat: int | None = None
    # The attacker a blocker blocks.
    target_uid: int | None = None
    # The value chosen for X.
    x: int | None = None
    # Human-readable description, for agents and the match log.
    label: str = field(default="", compare=False)
    # The name of the card the action is about, for sorting.
    card_name: str = field(default="", compare=False, repr=False)

    def sort_key(self) -> tuple:
        return (
            _RANK[self.kind],
            self.card_name,
            -1 if self.card_uid is None else self.card_uid,
            -1 if self.target_seat is None else self.target_seat,
            -1 if self.target_uid is None else self.target_uid,
            -1 if self.x is None else self.x,
        )

    def to_dict(self) -> dict:
        data = {"kind": self.kind, "label": self.label}
        if self.card_uid is not None:
            data["card_id"] = self.card_uid
        if self.target_seat is not None:
            data["target_seat"] = self.target_seat
        if self.target_uid is not None:
            data["target_id"] = self.target_uid
        if self.x is not None:
            data["x"] = self.x
        return data


PASS = Action(ActionKind.PASS, label="Pass")


def commander_tax(state: GameState, seat: int) -> int:
    return state.rules.commander_tax * state.player(seat).commander_casts


def commander_cost(state: GameState, seat: int) -> int | None:
    """What casting ``seat``'s commander costs now, or None if it isn't in the command zone."""
    player = state.player(seat)
    if not player.command:
        return None
    return player.command[0].spec.mana_cost.mana_value + commander_tax(state, seat)


def can_block(blocker: Permanent, attacker: Permanent) -> bool:
    if attacker.has("flying"):
        return blocker.has("flying") or blocker.has("reach")
    return True


def legal_actions(state: GameState, seat: int) -> list[Action]:
    """Every action ``seat`` may take right now; see the module docstring."""
    player = state.player(seat)
    if not player.alive:
        return [PASS]
    if state.declaring is not None:
        if seat != state.declaring_seat:
            return [PASS]
        if state.declaring == Declaring.ATTACKERS:
            return _attack_declarations(state, seat)
        return _block_declarations(state, seat)
    return _priority_actions(state, seat)


def _ordered(first: list[Action], rest: list[Action]) -> list[Action]:
    return first + sorted(rest, key=Action.sort_key)


def _distinct(cards):
    """One card per name: identical copies would only repeat the same option."""
    seen = {}
    for card in cards:
        if card.name not in seen or card.id < seen[card.name].id:
            seen[card.name] = card
    return [seen[name] for name in sorted(seen)]


def _priority_actions(state: GameState, seat: int) -> list[Action]:
    player = state.player(seat)
    actions = []
    sorcery_speed = seat == state.active_seat and state.step in MAIN_STEPS and not state.stack

    if sorcery_speed and player.lands_played < state.rules.lands_per_turn:
        actions += [
            Action(ActionKind.PLAY_LAND, card.id, label=f"Play land {card.name}", card_name=card.name)
            for card in _distinct(c for c in player.hand if c.spec.kind == Kind.LAND)
        ]

    if len(state.stack) < state.limits.max_stack_depth:
        sources = mana_sources(player)
        for card in _distinct(c for c in player.hand if c.spec.kind != Kind.LAND):
            if sorcery_speed or card.spec.instant_speed:
                actions += _casts(ActionKind.CAST_SPELL, card, sources, 0)
        for card in player.command:
            if sorcery_speed or card.spec.instant_speed:
                actions += _casts(ActionKind.CAST_COMMANDER, card, sources, commander_tax(state, seat))

    return _ordered([PASS], actions)


def _casts(kind: str, card, sources, tax: int) -> list[Action]:
    cost = card.spec.mana_cost
    printed = cost_text(cost)
    if kind == ActionKind.CAST_COMMANDER:
        what = f"Cast your commander {card.name} from the command zone"
        price = f"{printed} + {tax} tax" if tax else printed
    else:
        what = f"Cast {card.name}"
        price = printed
    if not cost.x:
        if not can_pay(sources, cost, tax):
            return []
        return [
            Action(kind, card.id, label=f"{what} ({price}, mana value {cost.mana_value})", card_name=card.name)
        ]
    actions = []
    for x in range(min(available_mana(sources), MAX_X) + 1):
        if not can_pay(sources, cost, tax + x * cost.x):
            break
        actions.append(
            Action(kind, card.id, x=x, label=f"{what} with X={x} ({price})", card_name=card.name)
        )
    return actions


def _attack_declarations(state: GameState, seat: int) -> list[Action]:
    player = state.player(seat)
    actions = []
    for perm in player.creatures():
        if perm.attacking is not None or not perm.can_tap_for_attack_or_mana or perm.has("defender"):
            continue
        for opponent in state.opponents(seat):
            actions.append(
                Action(
                    ActionKind.DECLARE_ATTACKER,
                    perm.id,
                    target_seat=opponent.seat,
                    label=(
                        f"Attack {opponent.deck_name} (seat {opponent.seat + 1}, {opponent.life} life) "
                        f"with {perm.name} ({perm.power}/{perm.toughness})"
                    ),
                    card_name=perm.name,
                )
            )
    end = Action(ActionKind.END_ATTACKERS, label="Done declaring attackers")
    return _ordered([end], actions)


def _block_declarations(state: GameState, seat: int) -> list[Action]:
    player = state.player(seat)
    attackers = [a for a in state.attackers() if a.attacking == seat]
    actions = []
    for blocker in player.creatures():
        if blocker.tapped:
            continue
        if blocker.blocking is not None:
            attacker = state.permanent(blocker.blocking)
            actions.append(
                Action(
                    ActionKind.UNDO_BLOCKER,
                    blocker.id,
                    label=f"Take back the block of {blocker.name} on {attacker.name}",
                    card_name=blocker.name,
                )
            )
            continue
        for attacker in attackers:
            if can_block(blocker, attacker):
                actions.append(
                    Action(
                        ActionKind.DECLARE_BLOCKER,
                        blocker.id,
                        target_uid=attacker.id,
                        label=(
                            f"Block {attacker.name} ({attacker.power}/{attacker.toughness}) "
                            f"with {blocker.name} ({blocker.power}/{blocker.toughness})"
                        ),
                        card_name=blocker.name,
                    )
                )
    menace_short = any(a.has("menace") and len(state.blockers_of(a.id)) == 1 for a in attackers)
    first = [] if menace_short else [Action(ActionKind.END_BLOCKERS, label="Done declaring blockers")]
    return _ordered(first, actions)
