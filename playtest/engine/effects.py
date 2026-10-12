"""Card effects: the DSL operations, targets, and static abilities.

* Static abilities (anthems, granted keywords) are a *derived view*: current
  power, toughness and keywords are computed from the printed card, counters,
  until-end-of-turn effects and every static ability on the battlefield, and
  never written onto the permanents.
* Selectors are resolved at the table actually present: "each opponent" is
  every living opponent, and a target belonging to a player who left the game
  is illegal.
* Targets are checked when chosen and again on resolution, against current
  characteristics (a "power 2 or less" restriction sees pump spells).

Pure: no Django, no I/O.
"""

from dataclasses import dataclass

from .cards import CardSpec
from .dsl import Ability, CardProgram, Op, SelKind, Target, Trigger
from .events import Event, EventKind
from .manacost import ManaCost
from .state import GameState, Permanent, Player, StackItem
from .zones import Zone, enter_battlefield, leave_battlefield, put_card

# -- references to players, permanents and stack items --------------------------


def player_ref(seat: int) -> str:
    return f"p{seat}"


def perm_ref(perm_id: int) -> str:
    return f"o{perm_id}"


def stack_ref(item_id: int) -> str:
    return f"s{item_id}"


def parse_ref(ref: str) -> tuple[str, int]:
    return ref[0], int(ref[1:])


def ref_label(state: GameState, ref: str) -> str:
    kind, value = parse_ref(ref)
    if kind == "p":
        player = state.player(value)
        return f"{player.name} (seat {value + 1})"
    if kind == "o":
        perm = state.permanent(value)
        if perm is None:
            return "a permanent that left"
        if perm.spec.is_creature:
            return f"{perm.name} {power(state, perm)}/{toughness(state, perm)} (seat {perm.controller + 1})"
        return f"{perm.name} (seat {perm.controller + 1})"
    item = state.stack_item(value)
    return f"{item.name} on the stack" if item is not None else "a spell that left"


# -- derived characteristics -----------------------------------------------------


def _static_sources(state: GameState):
    for source in state.battlefield:
        for ability in source.spec.abilities(Trigger.STATIC):
            yield source, ability


def _static_applies(state: GameState, source: Permanent, target: Target, perm: Permanent) -> bool:
    if not perm.spec.is_creature:
        return False
    match target.selector:
        case "SELF":
            return perm.id == source.id
        case "EACH_CREATURE_YOU_CONTROL":
            return perm.controller == source.controller
        case "EACH_OTHER_CREATURE_YOU_CONTROL":
            return perm.controller == source.controller and perm.id != source.id
        case "EACH_CREATURE_OPPONENTS_CONTROL":
            return perm.controller != source.controller
        case "EACH_OTHER_CREATURE":
            return perm.id != source.id
        case "EACH_CREATURE":
            return True
    return False


def _static_ops(state: GameState, perm: Permanent):
    for source, ability in _static_sources(state):
        for op in ability.ops:
            target = ability.target(op.target)
            if target is not None and _static_applies(state, source, target, perm):
                yield op


def power(state: GameState, perm: Permanent) -> int:
    value = (perm.spec.power or 0) + perm.plus_counters - perm.minus_counters + perm.eot_power
    value += sum(op.power for op in _static_ops(state, perm) if op.op == "MODIFY_PT")
    return value


def toughness(state: GameState, perm: Permanent) -> int:
    value = (perm.spec.toughness or 0) + perm.plus_counters - perm.minus_counters + perm.eot_toughness
    value += sum(op.toughness for op in _static_ops(state, perm) if op.op == "MODIFY_PT")
    return value


def keywords(state: GameState, perm: Permanent) -> set[str]:
    found = set(perm.spec.keywords) | set(perm.eot_keywords)
    found |= {op.keyword for op in _static_ops(state, perm) if op.op == "GRANT_KEYWORD"}
    return found


def has(state: GameState, perm: Permanent, keyword: str) -> bool:
    return keyword in keywords(state, perm)


# -- selectors and targets -------------------------------------------------------


def _is_type(perm: Permanent, *types: str) -> bool:
    return any(t in perm.spec.types for t in types)


def _object_matches(state: GameState, selector: str, perm: Permanent, controller: int) -> bool:
    match selector:
        case "ANY_TARGET" | "TARGET_CREATURE":
            return perm.spec.is_creature
        case "TARGET_CREATURE_YOU_CONTROL":
            return perm.spec.is_creature and perm.controller == controller
        case "TARGET_CREATURE_OPPONENT_CONTROLS":
            return perm.spec.is_creature and perm.controller != controller
        case "TARGET_CREATURE_OR_PLANESWALKER":
            return _is_type(perm, "creature", "planeswalker")
        case "TARGET_ARTIFACT_OR_ENCHANTMENT":
            return _is_type(perm, "artifact", "enchantment")
        case "TARGET_ARTIFACT":
            return _is_type(perm, "artifact")
        case "TARGET_ENCHANTMENT":
            return _is_type(perm, "enchantment")
        case "TARGET_NONLAND_PERMANENT":
            return not perm.spec.is_land
        case "TARGET_LAND":
            return perm.spec.is_land
        case "TARGET_PERMANENT":
            return True
    return False


def _restriction_ok(state: GameState, target: Target, perm: Permanent) -> bool:
    match target.restriction:
        case "":
            return True
        case "POWER_LE":
            return power(state, perm) <= target.restriction_value
        case "POWER_GE":
            return power(state, perm) >= target.restriction_value
        case "MV_LE":
            return perm.spec.mana_value <= target.restriction_value
        case "TAPPED":
            return perm.tapped
    return False


def _targetable(state: GameState, perm: Permanent, controller: int) -> bool:
    # Hexproof: no opponent can target it.
    return perm.controller == controller or not has(state, perm, "hexproof")


def legal_target(state: GameState, target: Target, ref: str, controller: int, item_id: int | None = None) -> bool:
    """Whether ``ref`` is (still) a legal choice for the targeted ``target``."""
    kind, value = parse_ref(ref)
    selector = target.selector
    spec_kind = target.spec.kind
    if kind == "p":
        if spec_kind not in (SelKind.PLAYER, SelKind.ANY) or not state.player(value).alive:
            return False
        return not (selector == "TARGET_OPPONENT" and value == controller)
    if kind == "o":
        perm = state.permanent(value)
        return (
            perm is not None
            and _object_matches(state, selector, perm, controller)
            and _restriction_ok(state, target, perm)
            and _targetable(state, perm, controller)
        )
    item = state.stack_item(value)
    if item is None or not item.is_spell or item.id == item_id or spec_kind != SelKind.SPELL:
        return False
    if selector == "TARGET_CREATURE_SPELL":
        return item.card.spec.is_creature
    if selector == "TARGET_NONCREATURE_SPELL":
        return not item.card.spec.is_creature
    return True


def target_choices(state: GameState, target: Target, controller: int, item_id: int | None = None) -> list[str]:
    """Every legal ref for a targeted selector, in a deterministic order."""
    refs = []
    if target.spec.kind in (SelKind.PLAYER, SelKind.ANY):
        refs += [player_ref(p.seat) for p in state.players if p.alive]
    if target.spec.kind in (SelKind.CREATURE, SelKind.PERMANENT, SelKind.ANY):
        refs += [perm_ref(p.id) for p in sorted(state.battlefield, key=lambda p: p.id)]
    if target.spec.kind == SelKind.SPELL:
        refs += [stack_ref(s.id) for s in state.stack]
    return [r for r in refs if legal_target(state, target, r, controller, item_id)]


def ward_cost(state: GameState, ref: str, controller: int) -> int:
    """Generic mana an opponent's ward asks for when ``ref`` is targeted."""
    kind, value = parse_ref(ref)
    if kind != "o":
        return 0
    perm = state.permanent(value)
    if perm is None or perm.controller == controller or not has(state, perm, "ward"):
        return 0
    return perm.spec.ward


def members(state: GameState, target: Target, controller: int, source_id: int | None) -> list[str]:
    """The refs an untargeted selector covers right now."""
    alive = [p.seat for p in state.players if p.alive]
    match target.selector:
        case "YOU":
            return [player_ref(controller)] if state.player(controller).alive else []
        case "EACH_OPPONENT":
            return [player_ref(s) for s in state.turn_order_from(controller) if s != controller]
        case "EACH_PLAYER":
            return [player_ref(s) for s in state.turn_order_from(controller)] if controller in alive else [player_ref(s) for s in alive]
        case "SELF":
            perm = state.permanent(source_id) if source_id is not None else None
            return [perm_ref(perm.id)] if perm is not None else []
    creatures = [p for p in sorted(state.battlefield, key=lambda p: p.id) if p.spec.is_creature]
    match target.selector:
        case "EACH_CREATURE":
            chosen = creatures
        case "EACH_OTHER_CREATURE":
            chosen = [p for p in creatures if p.id != source_id]
        case "EACH_CREATURE_YOU_CONTROL":
            chosen = [p for p in creatures if p.controller == controller]
        case "EACH_OTHER_CREATURE_YOU_CONTROL":
            chosen = [p for p in creatures if p.controller == controller and p.id != source_id]
        case "EACH_CREATURE_OPPONENTS_CONTROL":
            chosen = [p for p in creatures if p.controller != controller]
        case _:
            chosen = []
    return [perm_ref(p.id) for p in chosen]


def target_slots(abilities) -> list[tuple[str, Target]]:
    """The targets a spell or ability needs chosen, as ``(slot, target)``.

    A slot is ``"<ability index>:<key>"``: two abilities of one card may both
    call their target ``t1``.
    """
    return [(f"{i}:{t.key}", t) for i, ability in enumerate(abilities) for t in ability.targets if t.targeted]


# -- applying operations ---------------------------------------------------------


@dataclass
class EffectContext:
    controller: int
    source_name: str
    # The source permanent (for abilities), if any.
    source_id: int | None
    # Chosen targets, by slot (see :func:`target_slots`).
    targets: dict[str, str]
    x: int = 0
    # Keywords of the source that change how its damage works.
    deathtouch: bool = False
    lifelink: bool = False
    commander: bool = False


def _amount(op: Op, ctx: EffectContext) -> int:
    if op.amount == "X":
        return ctx.x
    return int(op.amount or 0)


def _refs(state: GameState, ability: Ability, op: Op, ctx: EffectContext, index: int) -> list[str]:
    target = ability.target(op.target)
    if target is None:
        return []
    if target.targeted:
        ref = ctx.targets.get(f"{index}:{target.key}")
        return [ref] if ref is not None else []
    return members(state, target, ctx.controller, ctx.source_id)


def draw_cards(state: GameState, seat: int, count: int) -> list[Event]:
    player = state.player(seat)
    drawn = 0
    for _ in range(count):
        if not player.library:
            player.drew_from_empty = True
            break
        card = player.library.pop(0)
        player.hand.append(card)
        drawn += 1
        if card.spec.is_spell_only and not card.spec.supported:
            player.dead_drawn += 1
    events = []
    if drawn:
        events.append(
            Event(EventKind.DRAW, seat, f"{player.name} draws {drawn} card{'s' if drawn > 1 else ''}.", {"count": drawn})
        )
    if player.drew_from_empty:
        events.append(Event(EventKind.WARNING, seat, f"{player.name} tried to draw from an empty library.", {"reason": "empty_library"}))
    return events


def discard_order(state: GameState, player: Player) -> list:
    """Which cards a player discards first when told to: excess lands, then the most expensive."""
    lands_out = sum(1 for p in state.battlefield if p.controller == player.seat and p.spec.is_land)
    return sorted(
        player.hand,
        key=lambda c: (not (c.spec.is_land and lands_out >= 5), -c.spec.mana_value, c.spec.is_land, c.name, c.id),
    )


def deal_damage(
    state: GameState, ref: str, amount: int, *, source_seat: int, source_name: str, deathtouch=False, combat=False, commander=False
) -> list[Event]:
    """Damage to a player (life loss) or a creature (marked damage)."""
    if amount <= 0:
        return []
    kind, value = parse_ref(ref)
    if kind == "p":
        player = state.player(value)
        player.life -= amount
        if commander:
            player.commander_damage[source_seat] = player.commander_damage.get(source_seat, 0) + amount
        return [
            Event(
                EventKind.DAMAGE,
                value,
                f"{source_name} deals {amount} damage to {player.name} ({player.life} life left).",
                {
                    "amount": amount,
                    "source": source_name,
                    "source_seat": source_seat,
                    "target_seat": value,
                    "life": player.life,
                    "combat": combat,
                    "commander": commander,
                },
            )
        ]
    perm = state.permanent(value)
    if perm is None:
        return []
    perm.damage += amount
    if deathtouch:
        perm.deathtouched = True
    return [
        Event(
            EventKind.DAMAGE,
            perm.controller,
            f"{source_name} deals {amount} damage to {perm.name}.",
            {"amount": amount, "source": source_name, "source_seat": source_seat, "creature": perm.name, "combat": combat},
        )
    ]


def gain_life(state: GameState, seat: int, amount: int, source_name: str) -> list[Event]:
    player = state.player(seat)
    player.life += amount
    return [
        Event(
            EventKind.EFFECT,
            seat,
            f"{player.name} gains {amount} life ({player.life}).",
            {"effect": "gain_life", "amount": amount, "life": player.life, "source": source_name},
        )
    ]


def _best_color(state: GameState, seat: int) -> str:
    """The color an "any color" mana is made in: the one the hand needs most."""
    counts = {c: 0 for c in "WUBRG"}
    for card in state.player(seat).hand:
        for pip in card.spec.cost.pips:
            for color in pip:
                if color in counts:
                    counts[color] += 1
    return max("WUBRG", key=lambda c: (counts[c], -"WUBRG".index(c)))


def token_spec(op: Op) -> CardSpec:
    subtype = op.subtype.title()
    return CardSpec(
        name=f"{subtype} token",
        types=("creature",),
        subtypes=tuple(op.subtype.split()),
        power=op.power,
        toughness=op.toughness,
        colors=op.colors,
        keywords=op.keywords,
        program=CardProgram(),
        cost=ManaCost(),
    )


def _sacrifice_choice(state: GameState, seat: int, what: str) -> list[Permanent]:
    def fits(perm: Permanent) -> bool:
        match what:
            case "creature":
                return perm.spec.is_creature
            case "land":
                return perm.spec.is_land
            case "nonland permanent":
                return not perm.spec.is_land
            case "permanent":
                return True
        return what in perm.spec.types

    candidates = [p for p in state.battlefield if p.controller == seat and fits(p)]
    # The player gives up their least valuable permanent first.
    return sorted(
        candidates,
        key=lambda p: (p.is_commander, power(state, p) + toughness(state, p), p.spec.mana_value, p.id),
    )


def apply_op(state: GameState, ability: Ability, op: Op, ctx: EffectContext, index: int = 0) -> list[Event]:
    """Carries out one operation of the ``index``-th ability; returns what happened."""
    events: list[Event] = []
    refs = _refs(state, ability, op, ctx, index)
    amount = _amount(op, ctx)
    name = ctx.source_name

    match op.op:
        case "DEAL_DAMAGE":
            total = 0
            for ref in refs:
                dealt = deal_damage(
                    state, ref, amount, source_seat=ctx.controller, source_name=name, deathtouch=ctx.deathtouch
                )
                total += amount if dealt else 0
                events += dealt
            if ctx.lifelink and total:
                events += gain_life(state, ctx.controller, total, name)
        case "GAIN_LIFE":
            for ref in refs:
                events += gain_life(state, parse_ref(ref)[1], amount, name)
        case "LOSE_LIFE":
            for ref in refs:
                player = state.player(parse_ref(ref)[1])
                player.life -= amount
                events.append(
                    Event(
                        EventKind.EFFECT,
                        player.seat,
                        f"{player.name} loses {amount} life ({player.life}).",
                        {"effect": "lose_life", "amount": amount, "life": player.life, "source": name, "source_seat": ctx.controller},
                    )
                )
        case "DRAW_CARDS":
            for ref in refs:
                events += draw_cards(state, parse_ref(ref)[1], amount)
        case "DISCARD_CARDS":
            for ref in refs:
                player = state.player(parse_ref(ref)[1])
                for card in discard_order(state, player)[:amount]:
                    player.hand.remove(card)
                    put_card(state, card, Zone.GRAVEYARD)
                    events.append(
                        Event(EventKind.DISCARD, player.seat, f"{player.name} discards {card.name}.", {"card": card.name})
                    )
        case "MILL":
            for ref in refs:
                player = state.player(parse_ref(ref)[1])
                milled = [player.library.pop(0) for _ in range(min(amount, len(player.library)))]
                for card in milled:
                    put_card(state, card, Zone.GRAVEYARD)
                events.append(
                    Event(EventKind.EFFECT, player.seat, f"{player.name} mills {len(milled)}.", {"effect": "mill", "amount": len(milled)})
                )
        case "DESTROY" | "EXILE" | "RETURN_TO_HAND":
            zone = {"DESTROY": Zone.GRAVEYARD, "EXILE": Zone.EXILE, "RETURN_TO_HAND": Zone.HAND}[op.op]
            for ref in refs:
                perm = state.permanent(parse_ref(ref)[1])
                if perm is None:
                    continue
                if op.op == "DESTROY" and has(state, perm, "indestructible"):
                    continue
                verb = {"DESTROY": "is destroyed", "EXILE": "is exiled", "RETURN_TO_HAND": "returns to its owner's hand"}[op.op]
                events.append(
                    Event(EventKind.EFFECT, perm.controller, f"{perm.name} {verb}.", {"effect": op.op.lower(), "card": perm.name, "source": name})
                )
                events += leave_battlefield(state, perm, zone)
        case "TAP" | "UNTAP":
            for ref in refs:
                perm = state.permanent(parse_ref(ref)[1])
                if perm is not None:
                    perm.tapped = op.op == "TAP"
                    events.append(
                        Event(EventKind.EFFECT, perm.controller, f"{perm.name} is {op.op.lower()}ped.", {"effect": op.op.lower(), "card": perm.name})
                    )
        case "ADD_MANA":
            pool = state.player(ctx.controller).mana_pool
            symbols = [op.mana[i + 1] for i in range(0, len(op.mana), 3)] if op.mana else [_best_color(state, ctx.controller)] * op.any_color
            for color in symbols:
                pool[color] = pool.get(color, 0) + 1
            events.append(
                Event(EventKind.EFFECT, ctx.controller, f"{name} adds {''.join('{' + c + '}' for c in symbols)}.", {"effect": "add_mana", "mana": symbols})
            )
        case "CREATE_TOKEN":
            spec = token_spec(op)
            for _ in range(amount):
                enter_battlefield(state, None, ctx.controller, spec)
            events.append(
                Event(
                    EventKind.EFFECT,
                    ctx.controller,
                    f"{state.player(ctx.controller).name} creates {amount} {op.power}/{op.toughness} {spec.name}{'s' if amount > 1 else ''}.",
                    {"effect": "create_token", "amount": amount, "token": spec.name},
                )
            )
        case "MODIFY_PT" | "GRANT_KEYWORD" | "PUT_COUNTERS":
            for ref in refs:
                perm = state.permanent(parse_ref(ref)[1])
                if perm is None or not perm.spec.is_creature:
                    continue
                if op.op == "MODIFY_PT":
                    perm.eot_power += op.power
                    perm.eot_toughness += op.toughness
                    text = f"{perm.name} gets {op.power:+d}/{op.toughness:+d} until end of turn."
                elif op.op == "GRANT_KEYWORD":
                    perm.eot_keywords.append(op.keyword)
                    text = f"{perm.name} gains {op.keyword} until end of turn."
                else:
                    if op.counter == "+1/+1":
                        perm.plus_counters += amount
                    else:
                        perm.minus_counters += amount
                    text = f"{perm.name} gets {amount} {op.counter} counter{'s' if amount > 1 else ''}."
                events.append(Event(EventKind.EFFECT, perm.controller, text, {"effect": op.op.lower(), "card": perm.name}))
        case "SACRIFICE":
            target = ability.target(op.target)
            if target.selector == "SELF":
                perm = state.permanent(ctx.source_id) if ctx.source_id is not None else None
                if perm is not None:
                    events.append(Event(EventKind.EFFECT, perm.controller, f"{perm.name} is sacrificed.", {"effect": "sacrifice", "card": perm.name}))
                    events += leave_battlefield(state, perm, Zone.GRAVEYARD)
            else:
                for ref in refs:
                    seat = parse_ref(ref)[1]
                    for perm in _sacrifice_choice(state, seat, op.what)[:amount]:
                        events.append(Event(EventKind.EFFECT, seat, f"{state.player(seat).name} sacrifices {perm.name}.", {"effect": "sacrifice", "card": perm.name}))
                        events += leave_battlefield(state, perm, Zone.GRAVEYARD)
        case "COUNTER_SPELL":
            for ref in refs:
                item = state.stack_item(parse_ref(ref)[1])
                if item is not None:
                    events += counter_item(state, item, f"countered by {name}")
    return events


def counter_item(state: GameState, item: StackItem, why: str) -> list[Event]:
    """Removes ``item`` from the stack without effect; a spell goes to its owner's graveyard."""
    if item in state.stack:
        state.stack.remove(item)
    if item.card is not None:
        put_card(state, item.card, Zone.GRAVEYARD)
    return [Event(EventKind.COUNTERED, item.controller, f"{item.name} is {why}.", {"card": item.name, "reason": why})]


def resolve_abilities(state: GameState, item: StackItem, abilities, ctx: EffectContext) -> tuple[list[Event], bool]:
    """Resolves ``abilities`` for ``item``, re-checking every target first.

    Returns the events and whether the item fizzled (every target illegal).
    """
    slots = target_slots(abilities)
    legal = {
        slot: ctx.targets[slot]
        for slot, target in slots
        if slot in ctx.targets and legal_target(state, target, ctx.targets[slot], item.controller, item.id)
    }
    if slots and not legal:
        return [Event(EventKind.FIZZLE, item.controller, f"{item.name} has no legal target left and does nothing.", {"card": item.name})], True
    ctx.targets = legal
    events: list[Event] = []
    for index, ability in enumerate(abilities):
        for op in ability.ops:
            target = ability.target(op.target)
            if target is not None and target.targeted and f"{index}:{target.key}" not in legal:
                # That target became illegal: this part does nothing.
                continue
            events += apply_op(state, ability, op, ctx, index)
    return events, False
