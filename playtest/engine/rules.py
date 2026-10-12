"""The rules the kernel enforces — and, by omission, the ones it does not.

Turn and priority:

* Untap, upkeep, draw, main, combat (beginning, declare attackers, declare
  blockers, first-strike damage, combat damage, end), second main, end step,
  cleanup. The first player of a two-player game skips their first draw.
* Priority starts with the active player and passes around the table in turn
  order. The top of the stack resolves, or the step ends, only when every
  player still in the game passes in succession. A player who acts keeps
  priority.
* Lands and sorcery-speed spells only in your own main phase with an empty
  stack; one land per turn. Instants and flash spells whenever you have
  priority.

Mana:

* Lands and mana rocks make the colors Scryfall lists for them; a source that
  makes two mana (Sol Ring) makes two. Sources with a cost (pay life, take
  damage, sacrifice it, or only while you control a land type) are spent only
  when the free ones cannot pay. Mana is paid **automatically**: the engine
  taps the sources that keep the most colors open. Players do not
  choose how to pay, so holding up a specific land is not a decision anyone
  makes.
* In Commander formats the commander is cast from the command zone for its
  cost plus 2 for each previous cast; if it would die or be exiled it returns
  to the command zone. Constructed formats have no commander.

Spells and abilities:

* Cards do what their compiled program says (damage, life, cards, removal,
  counterspells, tokens, pump, counters, mana, library searches, scry,
  surveil…). When an effect needs a choice (which card to find, what to keep
  on top), the player makes it. Targets are chosen when a
  spell is cast and checked again when it resolves: a spell whose every
  target is gone does nothing.
* Triggered abilities ("when this enters", "when this dies", "whenever this
  attacks", "at the beginning of your upkeep / end step") go on the stack
  before the next player gets priority, active player's first.
* A card the compiler could not read plays as a blank: a permanent is a
  vanilla object with its printed body and keywords, and an instant or
  sorcery cannot be cast at all.

Combat:

* Attackers are declared one at a time, each at one opponent; creatures that
  arrived this turn cannot attack unless they have haste. Each defending
  player then declares blockers against the creatures attacking them, one at
  a time.
* Flying can only be blocked by flying or reach; menace needs two blockers;
  first strike and double strike deal damage in a first step; trample
  assigns only the excess over lethal damage to blockers; deathtouch makes
  any damage lethal; lifelink gains its controller life; vigilance attacks
  without tapping.

Losing:

* A player loses at 0 life, after drawing from an empty library, or (in
  Commander) at the format's limit of combat damage from a single commander.
  A player who loses leaves the game with everything they own. The last
  player standing wins; a game that reaches the round limit is a draw.
* Opening hands use a deterministic London mulligan: keep 2–5 lands, at most
  two mulligans (the first is free with three or more players).

Not supported: layers, replacement effects, copy effects, planeswalkers,
auras and equipment, alternative costs, and choosing how to pay mana.
"""

from .actions import Action, ActionKind, ability_sets, commander_cost, legal_actions
from .effects import (
    EffectContext,
    deal_damage,
    discard_order,
    gain_life,
    has,
    power,
    resolve_abilities,
    target_slots,
    toughness,
    ward_cost,
    counter_item,
    first_option,
    player_ref,
    perm_ref,
)
from .events import Event, EventKind
from .limits import Limits
from .mana import pay
from .manacost import parse_mana_cost
from .state import GameState, PendingTarget, Player, StackItem
from .zones import Zone, enter_battlefield, leave_battlefield, put_card, remove_player_objects, take_from


class IllegalAction(Exception):
    """An agent tried something the rules do not allow."""


# -- applying actions ----------------------------------------------------------


def apply(state: GameState, seat: int, action: Action, limits: Limits = Limits()) -> list[Event]:
    """Carries out ``action`` for ``seat`` and returns what happened.

    Raises :class:`IllegalAction` unless ``action`` is one of the actions
    :func:`legal_actions` offers right now: whatever an agent answers, the
    kernel never applies an illegal action.
    """
    if action not in legal_actions(state, seat, limits):
        raise IllegalAction(f"Not a legal action now: {action.label or action}")
    player = state.player(seat)
    match action.kind:
        case ActionKind.PASS:
            return []
        case ActionKind.PLAY_LAND:
            card = take_from(player.hand, action.card)
            player.lands_played += 1
            enter_battlefield(state, card, seat)
            return [Event(EventKind.PLAY_LAND, seat, f"{player.name} plays {card.name}.", {"card": card.name})]
        case ActionKind.CAST | ActionKind.CAST_COMMANDER:
            return _cast(state, seat, action)
        case ActionKind.ACTIVATE:
            return _activate(state, seat, action)
        case ActionKind.DECLARE_ATTACKER:
            perm = state.permanent(action.permanent)
            perm.attacking = action.defender
            if not has(state, perm, "vigilance"):
                perm.tapped = True
            state.combat.steps += 1
            defender = state.player(action.defender)
            return [
                Event(
                    EventKind.ATTACK,
                    seat,
                    f"{perm.name} attacks {defender.name}.",
                    {"card": perm.name, "target_seat": action.defender, "power": power(state, perm)},
                )
            ]
        case ActionKind.END_ATTACKERS | ActionKind.END_BLOCKERS:
            state.combat = None
            return []
        case ActionKind.DECLARE_BLOCKER:
            blocker = state.permanent(action.permanent)
            attacker = state.permanent(action.attacker)
            blocker.blocking = attacker.id
            attacker.blocked_by.append(blocker.id)
            attacker.was_blocked = True
            state.combat.steps += 1
            return [
                Event(
                    EventKind.BLOCK,
                    seat,
                    f"{blocker.name} blocks {attacker.name}.",
                    {"card": blocker.name, "attacker": attacker.name, "attacker_seat": attacker.controller},
                )
            ]
        case ActionKind.UNDO_BLOCKER:
            blocker = state.permanent(action.permanent)
            attacker = state.permanent(blocker.blocking)
            blocker.blocking = None
            if attacker is not None:
                attacker.blocked_by.remove(blocker.id)
                attacker.was_blocked = bool(attacker.blocked_by)
            state.combat.steps += 1
            return []
        case ActionKind.CHOOSE_TARGET:
            return _choose_target(state, action)
        case ActionKind.CHOOSE:
            # The resolving effect reads the answer; nothing else changes.
            return []
    raise IllegalAction(f"Unknown action {action.kind!r}.")


def _missing_slot(item: StackItem) -> str | None:
    return next((slot for slot, _ in target_slots(item.abilities) if slot not in item.targets), None)


def _cast(state: GameState, seat: int, action: Action) -> list[Event]:
    player = state.player(seat)
    commander = action.kind == ActionKind.CAST_COMMANDER
    zone = player.command if commander else player.hand
    card = next(c for c in zone if c.id == action.card)
    base = commander_cost(state, seat, card.id) if commander else card.spec.cost
    ward = sum(ward_cost(state, ref, seat) for _, ref in action.targets)
    cost = base.with_x(action.x or 0).plus_generic(ward)
    paid = pay(state, seat, cost)
    take_from(zone, card.id)
    tax = state.rules.commander_tax * player.commander_casts.get(card.id, 0) if commander else 0
    if commander:
        player.commander_casts[card.id] = player.commander_casts.get(card.id, 0) + 1
    abilities = dict(ability_sets(card.spec))[action.mode]
    item = StackItem(
        id=state.new_id(),
        controller=seat,
        card=card,
        source_name=card.name,
        source_id=None,
        abilities=abilities,
        targets=dict(action.targets),
        x=action.x or 0,
        is_creature_spell=card.spec.is_creature,
    )
    state.stack.append(item)
    missing = _missing_slot(item)
    if missing is not None:
        state.pending_target = PendingTarget(item.id, missing)
    text = f"{player.name} casts {card.name}"
    if action.x is not None:
        text += f" with X={action.x}"
    return paid + [
        Event(
            EventKind.CAST,
            seat,
            text + (f" (commander tax {tax})." if tax else "."),
            {
                "card": card.name,
                "types": list(card.spec.types),
                "commander": commander,
                "cost": str(cost),
                "mana_value": card.spec.mana_value,
                "tax": tax,
                "x": action.x,
            },
        )
    ]


def _activate(state: GameState, seat: int, action: Action) -> list[Event]:
    perm = state.permanent(action.permanent)
    ability = perm.spec.program.abilities[action.ability]
    cost = ability.cost
    if cost.tap:
        perm.tapped = True
    ward = sum(ward_cost(state, ref, seat) for _, ref in action.targets)
    paid = pay(state, seat, parse_mana_cost(cost.mana).plus_generic(ward))
    perm.activations += 1
    events = paid + [Event(EventKind.ACTIVATE, seat, f"{state.player(seat).name} activates {perm.name}.", {"card": perm.name})]
    if cost.life:
        player = state.player(seat)
        player.life -= cost.life
        events.append(
            Event(
                EventKind.EFFECT,
                seat,
                f"{player.name} pays {cost.life} life ({player.life}).",
                {"effect": "lose_life", "amount": cost.life, "life": player.life, "source": perm.name, "source_seat": seat},
            )
        )
    if cost.discard:
        player = state.player(seat)
        for card in discard_order(state, player)[: cost.discard]:
            player.hand.remove(card)
            put_card(state, card, Zone.GRAVEYARD)
            events.append(Event(EventKind.DISCARD, seat, f"{player.name} discards {card.name}.", {"card": card.name}))
    if cost.sacrifice_self:
        events += leave_battlefield(state, perm, Zone.GRAVEYARD)
    item = StackItem(
        id=state.new_id(),
        controller=seat,
        card=None,
        source_name=perm.name,
        source_id=perm.id,
        abilities=(ability,),
        targets=dict(action.targets),
    )
    state.stack.append(item)
    missing = _missing_slot(item)
    if missing is not None:
        state.pending_target = PendingTarget(item.id, missing)
    return events


def _choose_target(state: GameState, action: Action) -> list[Event]:
    pending = state.pending_target
    item = state.stack_item(pending.item_id)
    item.targets[pending.key] = action.ref
    state.pending_target = None
    events = []
    ward = ward_cost(state, action.ref, item.controller)
    if ward:
        try:
            events += pay(state, item.controller, parse_mana_cost(f"{{{ward}}}"))
            events.append(Event(EventKind.EFFECT, item.controller, f"{state.player(item.controller).name} pays ward {{{ward}}}.", {"effect": "ward", "amount": ward}))
        except ValueError:
            return events + counter_item(state, item, "countered by ward")
    missing = _missing_slot(item)
    if missing is not None:
        state.pending_target = PendingTarget(item.id, missing)
    return events


# -- the stack -------------------------------------------------------------------


def resolve_top(state: GameState, choose=None) -> list[Event]:
    """Resolves the top of the stack; ``choose`` answers the choices effects ask for
    (the :class:`~.game.Game` asks the agents; default: the first option)."""
    item = state.stack.pop()
    controller = state.player(item.controller)
    if item.card is not None and item.card.spec.is_permanent:
        perm = enter_battlefield(state, item.card, item.controller)
        return [
            Event(
                EventKind.RESOLVE,
                item.controller,
                f"{item.name} enters the battlefield under {controller.name}'s control.",
                {"card": item.name, "permanent": perm.id},
            )
        ]
    source = state.permanent(item.source_id) if item.source_id is not None else None
    ctx = EffectContext(
        controller=item.controller,
        source_name=item.name,
        source_id=item.source_id,
        targets=dict(item.targets),
        x=item.x,
        deathtouch=bool(source and has(state, source, "deathtouch")),
        lifelink=bool(source and has(state, source, "lifelink")),
        choose=choose or first_option,
    )
    events = [Event(EventKind.RESOLVE, item.controller, f"{item.name} resolves.", {"card": item.name, "spell": item.is_spell})]
    effects, fizzled = resolve_abilities(state, item, item.abilities, ctx)
    if fizzled:
        events = effects
    else:
        events += effects
    if item.card is not None:
        put_card(state, item.card, Zone.GRAVEYARD)
    return events


def trigger_item(state: GameState, trigger) -> StackItem:
    return StackItem(
        id=state.new_id(),
        controller=trigger.controller,
        card=None,
        source_name=trigger.source_name,
        source_id=trigger.source_id,
        abilities=(trigger.ability,),
    )


# -- state-based actions ---------------------------------------------------------


def eliminate(state: GameState, seat: int, reason: str) -> list[Event]:
    player = state.player(seat)
    if not player.alive:
        return []
    player.eliminated_round = state.round
    remove_player_objects(state, seat)
    if state.pending_target is not None and state.stack_item(state.pending_target.item_id) is None:
        state.pending_target = None
    return [Event(EventKind.ELIMINATED, seat, f"{player.name} is eliminated: {reason}.", {"reason": reason})]


def _loss_reason(state: GameState, player: Player) -> str | None:
    if player.life <= 0:
        return "life total reached 0"
    if player.drew_from_empty:
        return "drew from an empty library"
    limit = state.rules.commander_damage_limit
    if limit is not None:
        for commander_id, damage in sorted(player.commander_damage.items()):
            if damage >= limit:
                owner, name = state.commanders[commander_id]
                return f"{damage} combat damage from {state.player(owner).name}'s commander {name}"
    return None


def check_state(state: GameState, limits: Limits = Limits()) -> list[Event]:
    """State-based actions, repeated until nothing changes (a dying anthem can kill)."""
    events: list[Event] = []
    for _ in range(limits.max_sba_iterations):
        changed = False
        losers = [(p.seat, reason) for p in state.alive_players() if (reason := _loss_reason(state, p))]
        for seat, reason in losers:
            events += eliminate(state, seat, reason)
            changed = True

        for perm in sorted(state.battlefield, key=lambda p: p.id):
            if perm.plus_counters and perm.minus_counters:
                cancel = min(perm.plus_counters, perm.minus_counters)
                perm.plus_counters -= cancel
                perm.minus_counters -= cancel
        dying = []
        for perm in sorted(state.battlefield, key=lambda p: p.id):
            if not perm.spec.is_creature:
                continue
            tough = toughness(state, perm)
            if tough <= 0:
                dying.append(perm)
            elif not has(state, perm, "indestructible") and (
                perm.damage >= tough or (perm.deathtouched and perm.damage > 0)
            ):
                dying.append(perm)
        legends: dict[tuple, list] = {}
        for perm in state.battlefield:
            if perm.spec.legendary:
                legends.setdefault((perm.controller, perm.name), []).append(perm)
        for group in legends.values():
            # The legend rule: the controller keeps the newest one.
            dying += sorted(group, key=lambda p: p.id)[:-1]
        for perm in dying:
            if perm in state.battlefield:
                events += leave_battlefield(state, perm, Zone.GRAVEYARD)
                changed = True
        if not changed:
            return events
    events.append(Event(EventKind.LIMIT, None, "State-based actions did not settle; moving on.", {"limit": "sba_iterations"}))
    return events


# -- turn-based actions ----------------------------------------------------------


def untap_step(state: GameState, seat: int) -> None:
    for perm in state.battlefield:
        perm.activations = 0
        if perm.controller == seat:
            perm.tapped = False
            perm.sick = False
    state.player(seat).lands_played = 0


def cleanup_step(state: GameState, seat: int) -> list[Event]:
    player = state.player(seat)
    events = []
    excess = len(player.hand) - state.rules.max_hand_size
    if excess > 0:
        for card in discard_order(state, player)[:excess]:
            player.hand.remove(card)
            put_card(state, card, Zone.GRAVEYARD)
            events.append(Event(EventKind.DISCARD, seat, f"{player.name} discards {card.name} to hand size.", {"card": card.name}))
    for perm in state.battlefield:
        perm.damage = 0
        perm.deathtouched = False
        perm.eot_power = perm.eot_toughness = 0
        perm.eot_keywords.clear()
    return events


def empty_mana_pools(state: GameState) -> None:
    for player in state.players:
        player.mana_pool.clear()


# -- combat damage -----------------------------------------------------------------


def has_first_strikers(state: GameState) -> bool:
    return any(
        (p.attacking is not None or p.blocking is not None)
        and (has(state, p, "first strike") or has(state, p, "double strike"))
        for p in state.battlefield
    )


def _deals_now(state: GameState, perm, first_strike_step: bool, dealt_first: set[int]) -> bool:
    first = has(state, perm, "first strike")
    double = has(state, perm, "double strike")
    if first_strike_step:
        return first or double
    return double or perm.id not in dealt_first


def combat_damage(state: GameState, first_strike_step: bool, dealt_first: set[int]) -> list[Event]:
    """Assigns and deals combat damage, all of it simultaneously."""
    assignments = []  # (source, ref, amount)
    attackers = sorted((p for p in state.battlefield if p.attacking is not None), key=lambda p: p.id)
    for attacker in attackers:
        if not _deals_now(state, attacker, first_strike_step, dealt_first):
            continue
        remaining = power(state, attacker)
        if remaining <= 0:
            continue
        defender = state.player(attacker.attacking)
        trample = has(state, attacker, "trample")
        blockers = [b for b in (state.permanent(i) for i in attacker.blocked_by) if b is not None]
        if not attacker.was_blocked:
            if defender.alive:
                assignments.append((attacker, player_ref(defender.seat), remaining))
            continue
        for index, blocker in enumerate(blockers):
            last = index == len(blockers) - 1
            lethal = 1 if has(state, attacker, "deathtouch") else max(0, toughness(state, blocker) - blocker.damage)
            given = remaining if last and not trample else min(remaining, lethal)
            if given:
                assignments.append((attacker, perm_ref(blocker.id), given))
            remaining -= given
        if trample and remaining > 0 and defender.alive:
            assignments.append((attacker, player_ref(defender.seat), remaining))
    blockers = sorted((p for p in state.battlefield if p.blocking is not None), key=lambda p: p.id)
    for blocker in blockers:
        attacker = state.permanent(blocker.blocking)
        if attacker is None or not _deals_now(state, blocker, first_strike_step, dealt_first):
            continue
        amount = power(state, blocker)
        if amount > 0:
            assignments.append((blocker, perm_ref(attacker.id), amount))

    if first_strike_step:
        # Every first striker had its damage step, even one that dealt none (CR 510.4).
        dealt_first.update(
            p.id
            for p in state.battlefield
            if (p.attacking is not None or p.blocking is not None) and has(state, p, "first strike")
        )
    events: list[Event] = []
    lifelink: dict[int, tuple] = {}
    for source, ref, amount in assignments:
        if first_strike_step:
            dealt_first.add(source.id)
        commander = source.is_commander and ref.startswith("p")
        if commander:
            state.commanders.setdefault(source.card.id, (source.owner, source.name))
        events += deal_damage(
            state,
            ref,
            amount,
            source_seat=source.owner if commander else source.controller,
            commander_id=source.card.id if commander else None,
            source_name=source.name,
            deathtouch=has(state, source, "deathtouch"),
            combat=True,
            commander=commander,
        )
        if has(state, source, "lifelink"):
            total, perm = lifelink.get(source.id, (0, source))
            lifelink[source.id] = (total + amount, perm)
    for total, source in lifelink.values():
        events += gain_life(state, source.controller, total, source.name)
    return events


def end_combat(state: GameState) -> None:
    for perm in state.battlefield:
        perm.attacking = None
        perm.blocking = None
        perm.blocked_by = []
        perm.was_blocked = False
    state.combat = None


# -- opening hands -------------------------------------------------------------------


def _lands(cards) -> int:
    return sum(1 for c in cards if c.spec.is_land)


def london_mulligan(state: GameState, seat: int, rng, *, free_first: bool, max_mulligans: int = 2) -> list[Event]:
    """Draws an opening hand and mulligans it deterministically, never an agent decision.

    Keep with 2 to 5 lands; otherwise shuffle it back and draw seven again, at
    most ``max_mulligans`` times, then put one card per (non-free) mulligan on
    the bottom: spare lands from a flooded hand, the most expensive spells
    otherwise.
    """
    player = state.player(seat)
    size = state.rules.opening_hand
    events = []
    taken = 0
    while True:
        player.hand = player.library[:size]
        del player.library[:size]
        lands = _lands(player.hand)
        if 2 <= lands <= 5 or taken == max_mulligans:
            break
        taken += 1
        events.append(
            Event(EventKind.MULLIGAN, seat, f"{player.name} mulligans a {lands}-land hand.", {"lands": lands, "mulligan": taken})
        )
        player.library += player.hand
        player.hand = []
        rng.shuffle(player.library)
    bottom = max(0, taken - (1 if free_first else 0))
    if bottom:
        flooded = _lands(player.hand) > 4
        order = sorted(
            player.hand,
            key=lambda c: (not (c.spec.is_land and flooded), -c.spec.mana_value if not flooded else 0, c.name, c.id),
        )
        for card in order[:bottom]:
            player.hand.remove(card)
            player.library.append(card)
        events.append(
            Event(EventKind.MULLIGAN, seat, f"{player.name} keeps {len(player.hand)} cards.", {"kept": len(player.hand), "bottom": bottom})
        )
    player.dead_drawn = sum(1 for c in player.hand if c.spec.is_spell_only and not c.spec.supported)
    return events
