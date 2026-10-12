"""The choices an agent is offered, and how they are listed.

The contract of :func:`legal_actions`, tested as such:

* the first action is always the do-nothing one: ``Pass`` in a priority
  window, ``End attackers`` / ``End blockers`` during declarations (menace can
  withhold ``End blockers``, then the first action is a declaration);
* the order is deterministic — sorted, never set iteration;
* every action is legal right now, and applying it never raises;
* it is pure: it reads the state and never changes it.

Combat is declared one creature at a time, so a decision lists a sum of
options, never the ``2^n`` subsets of attackers. Identical objects (same name,
same state) are offered once.
"""

from dataclasses import dataclass, field
from itertools import combinations, product

from .cards import CardSpec
from .dsl import Trigger, describe_ability
from .effects import (
    has,
    keywords,
    parse_ref,
    power,
    ref_label,
    target_choices,
    target_slots,
    toughness,
    ward_cost,
)
from .limits import Limits
from .mana import can_pay, sources
from .manacost import ManaCost, parse_mana_cost
from .state import Card, GameState, Permanent


class ActionKind:
    PASS = "pass"
    PLAY_LAND = "play_land"
    CAST = "cast"
    CAST_COMMANDER = "cast_commander"
    ACTIVATE = "activate"
    DECLARE_ATTACKER = "declare_attacker"
    END_ATTACKERS = "end_attackers"
    DECLARE_BLOCKER = "declare_blocker"
    UNDO_BLOCKER = "undo_blocker"
    END_BLOCKERS = "end_blockers"
    CHOOSE_TARGET = "choose_target"
    # An answer to a choice an effect asks for while it resolves.
    CHOOSE = "choose"
    # Cycling or landcycling a card from hand.
    CYCLE = "cycle"


# The do-nothing actions: what a forced or capped decision falls back to.
NOTHING = (ActionKind.PASS, ActionKind.END_ATTACKERS, ActionKind.END_BLOCKERS)

_RANK = {
    ActionKind.PASS: 0,
    ActionKind.END_ATTACKERS: 0,
    ActionKind.END_BLOCKERS: 0,
    ActionKind.PLAY_LAND: 1,
    ActionKind.CAST_COMMANDER: 2,
    ActionKind.CAST: 3,
    ActionKind.ACTIVATE: 4,
    ActionKind.CYCLE: 4,
    ActionKind.DECLARE_ATTACKER: 5,
    ActionKind.DECLARE_BLOCKER: 5,
    ActionKind.UNDO_BLOCKER: 6,
    ActionKind.CHOOSE_TARGET: 7,
    ActionKind.CHOOSE: 8,
}


@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    # The card played or cast (hand or command zone).
    card: int | None = None
    # The permanent activating, attacking or blocking.
    permanent: int | None = None
    # Index of the activated ability in the card's program.
    ability: int | None = None
    # Index (in the program) of the mode chosen for a modal spell.
    mode: int | None = None
    x: int | None = None
    # Chosen targets: (slot, ref) pairs, sorted by slot.
    targets: tuple[tuple[str, str], ...] = ()
    defender: int | None = None
    attacker: int | None = None
    # CHOOSE_TARGET: the ref chosen.
    ref: str | None = None
    # How a spell is paid for beyond mana: "alt:<index>" (an alternative cost)
    # or "flashback"; and the cards or permanents that cost uses (exiled from
    # hand, returned, sacrificed).
    via: str | None = None
    paid_with: tuple[int, ...] = ()
    # Human-readable description, for agents and the match log.
    label: str = field(default="", compare=False)
    # The card's name, for sorting and agents.
    name: str = field(default="", compare=False)

    def to_dict(self) -> dict:
        data = {"kind": self.kind, "label": self.label}
        for key in ("card", "permanent", "ability", "mode", "x", "defender", "attacker", "ref", "via"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.targets:
            data["targets"] = dict(self.targets)
        if self.paid_with:
            data["paid_with"] = list(self.paid_with)
        return data

    @property
    def is_nothing(self) -> bool:
        return self.kind in NOTHING


def commander_cost(state: GameState, seat: int, card_id: int | None = None) -> ManaCost | None:
    """What casting one of ``seat``'s commanders costs now (default: the first in the
    command zone), or None if it isn't there. Each commander pays its own tax."""
    player = state.player(seat)
    card = next((c for c in player.command if card_id is None or c.id == card_id), None)
    if card is None:
        return None
    return card.spec.cost.plus_generic(state.rules.commander_tax * player.commander_casts.get(card.id, 0))


def ability_sets(spec: CardSpec) -> list[tuple[int | None, tuple]]:
    """The abilities a spell puts on the stack, per mode: ``[(mode, abilities)]``.

    A permanent spell carries none (its abilities work once it is a permanent).
    """
    if not spec.is_spell_only:
        return [(None, ())]
    abilities = spec.spell_abilities
    fixed = tuple(a for a in abilities if a.mode_group is None)
    groups = sorted({a.mode_group for a in abilities if a.mode_group is not None})
    if not groups:
        return [(None, fixed)]
    program = spec.program.abilities
    first = [a for a in abilities if a.mode_group == groups[0]]
    # Only the first modal group is a choice; a second one would be a second "choose".
    return [(program.index(mode), fixed + (mode,)) for mode in first]


def _can_cast_now(state: GameState, seat: int, spec: CardSpec) -> bool:
    if spec.is_instant or spec.has("flash"):
        return True
    return seat == state.active_seat and state.sorcery_window


def _target_combos(state: GameState, seat: int, abilities, limits: Limits):
    """The target combinations to enumerate, or None when there are too many.

    Returns ``[]`` when some slot has no legal target: the spell cannot be cast.
    """
    slots = target_slots(abilities)
    if not slots:
        return [()]
    choices = [target_choices(state, target, seat) for _, target in slots]
    if any(not c for c in choices):
        return []
    size = 1
    for c in choices:
        size *= len(c)
    if size > limits.max_enumerated_targets:
        return None
    return [tuple(zip([slot for slot, _ in slots], combo)) for combo in product(*choices)]


def _targets_label(state: GameState, targets) -> str:
    if not targets:
        return ""
    return " targeting " + ", ".join(ref_label(state, ref) for _, ref in targets)


def _fits_sacrifice(perm: Permanent, what: str) -> bool:
    match what:
        case "artifact or creature":
            return perm.spec.is_creature or "artifact" in perm.spec.types
        case "nonland permanent":
            return not perm.spec.is_land
        case "permanent":
            return True
    return what in perm.spec.types


def alt_cost_usable(state: GameState, seat: int, alt) -> bool:
    if alt.condition == "NOT_YOUR_TURN" and state.active_seat == seat:
        return False
    if alt.condition == "CONTROL_COMMANDER" and not any(p.is_commander for p in state.controlled(seat)):
        return False
    # CR 119.4: life can be paid only up to the life total.
    return state.player(seat).life >= alt.life


def _payments(state: GameState, seat: int, card: Card, base: ManaCost, via: str | None) -> list[tuple]:
    """The ways to pay for ``card``: ``(via, paid_with, mana cost, label)``."""
    spec = card.spec
    player = state.player(seat)
    ways = [(via, (), base, " for its flashback cost" if via == "flashback" else "")]
    if via is None:
        for index, alt in enumerate(spec.alt_costs):
            if not alt_cost_usable(state, seat, alt):
                continue
            pitches = [()]
            if alt.exile_count:
                pool, seen = [], set()
                for other in sorted(player.hand, key=lambda c: c.id):
                    if other.id != card.id and alt.exile_color in other.spec.colors:
                        pool.append(other)
                pitches = []
                for combo in combinations(pool, alt.exile_count):
                    names = tuple(sorted(c.name for c in combo))
                    if names not in seen:
                        seen.add(names)
                        pitches.append(combo)
            bounces = [None]
            if alt.return_land:
                bounces = [p for p in _distinct(state, state.controlled(seat)) if p.spec.is_land and alt.return_land in p.spec.subtypes]
            for pitch in pitches:
                for bounce in bounces:
                    parts = [f"paying {alt.life} life"] if alt.life else []
                    if pitch:
                        parts.append("exiling " + " and ".join(c.name for c in pitch))
                    if bounce is not None:
                        parts.append(f"returning {bounce.name}")
                    label = " by " + " and ".join(parts) if parts else " without paying its mana cost"
                    paid = tuple(c.id for c in pitch) + ((bounce.id,) if bounce is not None else ())
                    ways.append((f"alt:{index}", paid, ManaCost(), label))
    if not spec.extra_sacrifice:
        return ways
    victims = [p for p in _distinct(state, state.controlled(seat)) if _fits_sacrifice(p, spec.extra_sacrifice)]
    return [
        (way, paid + (victim.id,), cost, label + f", sacrificing {victim.name}")
        for way, paid, cost, label in ways
        for victim in victims
    ]


def _spell_actions(
    state: GameState, seat: int, card: Card, kind: str, base: ManaCost, limits: Limits, via: str | None = None
) -> list[Action]:
    spec = card.spec
    srcs = sources(state, seat)
    total = sum(s.amount for s in srcs)
    actions = []
    for mode, abilities in ability_sets(spec):
        combos = _target_combos(state, seat, abilities, limits)
        if combos == []:
            continue
        mode_label = ""
        if mode is not None:
            mode_label = f" — mode: {describe_ability(spec.program.abilities[mode])}"
        for way, paid_with, way_cost, way_label in _payments(state, seat, card, base, via):
            # An alternative cost replaces the whole mana cost, X included (X is 0).
            xs = range(0, max(0, min(total - way_cost.total(), limits.max_x)) + 1) if way_cost.x else [None]
            for x in xs:
                cost = way_cost.with_x(x or 0)
                for targets in combos if combos is not None else [()]:
                    ward = sum(ward_cost(state, ref, seat) for _, ref in targets)
                    if not can_pay(srcs, cost.plus_generic(ward)):
                        continue
                    verb = "Cast your commander" if kind == ActionKind.CAST_COMMANDER else "Cast"
                    label = f"{verb} {spec.name} ({cost.plus_generic(ward)}){way_label}"
                    if x is not None:
                        label += f" with X={x}"
                    label += mode_label
                    label += _targets_label(state, targets) if combos is not None else " (targets chosen next)"
                    actions.append(
                        Action(
                            kind, card=card.id, mode=mode, x=x, targets=targets, via=way, paid_with=paid_with, label=label, name=spec.name
                        )
                    )
    return actions


def _signature(state: GameState, perm: Permanent) -> tuple:
    """Two permanents with the same signature are interchangeable choices."""
    return (
        perm.name,
        perm.controller,
        perm.tapped,
        perm.sick,
        perm.damage,
        power(state, perm) if perm.spec.is_creature else None,
        toughness(state, perm) if perm.spec.is_creature else None,
        tuple(sorted(keywords(state, perm))),
        perm.attacking,
        perm.blocking,
        perm.activations,
        perm.is_commander,
    )


def _distinct(state: GameState, perms: list[Permanent]) -> list[Permanent]:
    seen = set()
    out = []
    for perm in sorted(perms, key=lambda p: p.id):
        signature = _signature(state, perm)
        if signature not in seen:
            seen.add(signature)
            out.append(perm)
    return out


def can_attack(state: GameState, perm: Permanent) -> bool:
    return (
        perm.controller == state.active_seat
        and perm.spec.is_creature
        and not perm.tapped
        and perm.attacking is None
        and not (perm.sick and not has(state, perm, "haste"))
        and not has(state, perm, "defender")
    )


def can_block(state: GameState, blocker: Permanent, attacker: Permanent) -> bool:
    if not blocker.spec.is_creature or blocker.tapped or blocker.blocking is not None:
        return False
    if has(state, attacker, "flying") and not (has(state, blocker, "flying") or has(state, blocker, "reach")):
        return False
    return True


def creature_label(state: GameState, perm: Permanent) -> str:
    kws = sorted(keywords(state, perm))
    extra = f", {', '.join(kws)}" if kws else ""
    return f"{perm.name} {power(state, perm)}/{toughness(state, perm)}{extra}"


def _priority_actions(state: GameState, seat: int, limits: Limits) -> list[Action]:
    player = state.player(seat)
    actions = [Action(ActionKind.PASS, label="Pass priority")]
    if state.sorcery_window and seat == state.active_seat and player.lands_played < state.rules.lands_per_turn:
        seen = set()
        for card in sorted(player.hand, key=lambda c: c.id):
            if card.spec.is_land and card.name not in seen:
                seen.add(card.name)
                tapped = " (enters tapped)" if card.spec.enters_tapped else ""
                actions.append(Action(ActionKind.PLAY_LAND, card=card.id, label=f"Play land {card.name}{tapped}", name=card.name))

    if len(state.stack) < limits.max_stack_depth:
        seen = set()
        for card in sorted(player.hand, key=lambda c: c.id):
            spec = card.spec
            if spec.is_land or card.name in seen or not _can_cast_now(state, seat, spec):
                continue
            seen.add(card.name)
            if spec.is_spell_only and not spec.supported:
                continue
            actions += _spell_actions(state, seat, card, ActionKind.CAST, spec.cost, limits)
        seen = set()
        for card in sorted(player.graveyard, key=lambda c: c.id):
            spec = card.spec
            if not spec.flashback or not spec.supported or card.name in seen or not _can_cast_now(state, seat, spec):
                continue
            seen.add(card.name)
            actions += _spell_actions(state, seat, card, ActionKind.CAST, parse_mana_cost(spec.flashback), limits, via="flashback")
        actions += _cycling(state, seat)
        for commander in sorted(player.command, key=lambda c: c.id):
            cost = commander_cost(state, seat, commander.id)
            if _can_cast_now(state, seat, commander.spec) and (commander.spec.supported or not commander.spec.is_spell_only):
                actions += _spell_actions(state, seat, commander, ActionKind.CAST_COMMANDER, cost, limits)
        actions += _activations(state, seat, limits)
    return actions


def _cycling(state: GameState, seat: int) -> list[Action]:
    actions, seen = [], set()
    srcs = sources(state, seat)
    for card in sorted(state.player(seat).hand, key=lambda c: c.id):
        spec = card.spec
        if not spec.cycling or card.name in seen or not can_pay(srcs, parse_mana_cost(spec.cycling)):
            continue
        seen.add(card.name)
        what = "find a " + spec.landcycling.removeprefix("TYPES:").replace("_", " ").lower() if spec.landcycling else "draw a card"
        actions.append(Action(ActionKind.CYCLE, card=card.id, label=f"Cycle {spec.name} ({spec.cycling}): discard it, {what}", name=spec.name))
    return actions


def _activations(state: GameState, seat: int, limits: Limits) -> list[Action]:
    actions = []
    for perm in _distinct(state, state.controlled(seat)):
        program = perm.spec.program
        if program is None or perm.activations >= limits.max_activations_per_permanent_per_turn:
            continue
        for index, ability in enumerate(program.abilities):
            if ability.trigger != Trigger.ACTIVATED or ability.is_mana_ability:
                continue
            cost = ability.cost
            if cost.tap and (perm.tapped or (perm.spec.is_creature and perm.sick and not has(state, perm, "haste"))):
                continue
            if cost.discard and len(state.player(seat).hand) < cost.discard:
                continue
            if cost.life and state.player(seat).life < cost.life:
                # CR 119.4: a player can pay life only if their life total is at least that much.
                continue
            srcs = [s for s in sources(state, seat) if not (cost.tap and s.permanent_id == perm.id)]
            mana = parse_mana_cost(cost.mana)
            combos = _target_combos(state, seat, (ability,), limits)
            if combos == []:
                continue
            for targets in combos if combos is not None else [()]:
                ward = sum(ward_cost(state, ref, seat) for _, ref in targets)
                if not can_pay(srcs, mana.plus_generic(ward)):
                    continue
                label = f"Activate {perm.name}: {describe_ability(ability)}"
                label += _targets_label(state, targets) if combos is not None else " (targets chosen next)"
                actions.append(
                    Action(ActionKind.ACTIVATE, permanent=perm.id, ability=index, targets=targets, label=label, name=perm.name)
                )
    return actions


def _attacker_actions(state: GameState, seat: int) -> list[Action]:
    actions = [Action(ActionKind.END_ATTACKERS, label="Done declaring attackers")]
    defenders = [s for s in state.turn_order_from(seat) if s != seat]
    for perm in _distinct(state, [p for p in state.battlefield if can_attack(state, p)]):
        for defender in defenders:
            target = state.player(defender)
            actions.append(
                Action(
                    ActionKind.DECLARE_ATTACKER,
                    permanent=perm.id,
                    defender=defender,
                    label=f"Attack {target.name} (seat {defender + 1}, {target.life} life) with {creature_label(state, perm)}",
                    name=perm.name,
                )
            )
    return actions


def menace_unsatisfied(state: GameState, defender: int) -> bool:
    return any(
        p.attacking == defender and has(state, p, "menace") and len(p.blocked_by) == 1 for p in state.battlefield
    )


def _blocker_actions(state: GameState, defender: int) -> list[Action]:
    actions = []
    if not menace_unsatisfied(state, defender):
        actions.append(Action(ActionKind.END_BLOCKERS, label="Done declaring blockers"))
    attackers = sorted((p for p in state.battlefield if p.attacking == defender), key=lambda p: p.id)
    own = [p for p in state.battlefield if p.controller == defender]
    for blocker in _distinct(state, [p for p in own if p.spec.is_creature and not p.tapped and p.blocking is None]):
        for attacker in attackers:
            if can_block(state, blocker, attacker):
                actions.append(
                    Action(
                        ActionKind.DECLARE_BLOCKER,
                        permanent=blocker.id,
                        attacker=attacker.id,
                        label=f"Block {creature_label(state, attacker)} with {creature_label(state, blocker)}",
                        name=blocker.name,
                    )
                )
    for blocker in sorted((p for p in own if p.blocking is not None), key=lambda p: p.id):
        attacker = state.permanent(blocker.blocking)
        actions.append(
            Action(
                ActionKind.UNDO_BLOCKER,
                permanent=blocker.id,
                label=f"Take back {blocker.name}'s block of {attacker.name if attacker else 'an attacker'}",
                name=blocker.name,
            )
        )
    return actions


def _target_actions(state: GameState) -> list[Action]:
    pending = state.pending_target
    item = state.stack_item(pending.item_id)
    if item is None:
        return []
    target = dict(target_slots(item.abilities))[pending.key]
    return [
        Action(ActionKind.CHOOSE_TARGET, ref=ref, label=f"Target {ref_label(state, ref)} with {item.name}", name=item.name)
        for ref in target_choices(state, target, item.controller, item.id)
    ]


def _sort_key(action: Action):
    return (
        _RANK[action.kind],
        action.name,
        action.card or 0,
        action.permanent or 0,
        action.ability if action.ability is not None else -1,
        action.mode if action.mode is not None else -1,
        action.x if action.x is not None else -1,
        action.defender if action.defender is not None else -1,
        action.attacker or 0,
        action.targets,
        action.ref or "",
        action.via or "",
        action.paid_with,
    )


def legal_actions(state: GameState, seat: int, limits: Limits = Limits()) -> list[Action]:
    """Every action ``seat`` may take now (see the module docstring for the contract)."""
    if state.pending_choice is not None:
        # The options keep the order the effect gave them (keys sort that way).
        pending = state.pending_choice
        if seat != pending.seat:
            return []
        return [Action(ActionKind.CHOOSE, ref=o.key, label=o.label) for o in pending.options]
    if state.pending_target is not None:
        actions = _target_actions(state)
    elif state.combat is not None and state.combat.phase == "attackers":
        actions = _attacker_actions(state, seat)
    elif state.combat is not None and state.combat.phase == "blockers":
        actions = _blocker_actions(state, seat)
    else:
        actions = _priority_actions(state, seat, limits)
    return sorted(actions, key=_sort_key)


def dead_spells(state: GameState, seat: int) -> list[Card]:
    """Unsupported instants and sorceries ``seat`` could cast now, were they supported."""
    if state.pending_target is not None or state.pending_choice is not None or state.combat is not None:
        return []
    srcs = sources(state, seat)
    return [
        card
        for card in sorted(state.player(seat).hand, key=lambda c: c.id)
        if card.spec.is_spell_only
        and not card.spec.supported
        and _can_cast_now(state, seat, card.spec)
        and can_pay(srcs, card.spec.cost.with_x(0))
    ]


def target_ref_owner(state: GameState, ref: str) -> int | None:
    """The seat a ref belongs to: the player, or the controller of the object."""
    kind, value = parse_ref(ref)
    if kind == "p":
        return value
    if kind == "o":
        perm = state.permanent(value)
        return perm.controller if perm else None
    item = state.stack_item(value)
    return item.controller if item else None

