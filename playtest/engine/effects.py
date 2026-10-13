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

from dataclasses import dataclass, field
from typing import Callable

from .cardrules import ManaAbility
from .cards import CardSpec
from .dsl import Ability, CardProgram, Cost, Op, SelKind, Target, Trigger, describe_op
from .events import Event, EventKind
from .manacost import ManaCost
from .state import ChoiceOption, GameState, Permanent, Player, StackItem
from .zones import Zone, enter_battlefield, leave_battlefield, put_card, queue_event_triggers

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
        case "TARGET_NONBASIC_LAND":
            return perm.spec.is_land and "basic" not in perm.spec.supertypes
        case "TARGET_PERMANENT":
            return True
    return False


def _restriction_ok(state: GameState, target: Target, perm: Permanent, controller: int) -> bool:
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
        case "LEGENDARY":
            return "legendary" in perm.spec.supertypes
        case "OPPONENT":
            return perm.controller != controller
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
            and _restriction_ok(state, target, perm, controller)
            and _targetable(state, perm, controller)
        )
    item = state.stack_item(value)
    if item is None or not item.is_spell or item.id == item_id or spec_kind != SelKind.SPELL:
        return False
    if selector == "TARGET_CREATURE_SPELL":
        return item.card.spec.is_creature
    if selector == "TARGET_NONCREATURE_SPELL":
        return not item.card.spec.is_creature
    if selector == "TARGET_INSTANT_OR_SORCERY_SPELL":
        return item.card.spec.is_spell_only
    if target.restriction == "MV_EQ":
        return item.card.spec.mana_value == target.restriction_value
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


def first_option(state: GameState, seat: int, kind: str, prompt: str, options: list[ChoiceOption]) -> str:
    """The chooser used outside a :class:`~.game.Game` (unit tests): the first option."""
    return options[0].key


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
    # Asks the agent of ``seat`` to pick one of ``options``; returns its key.
    choose: Callable = first_option
    # Last known information of each chosen target, by slot, taken as the
    # spell or ability starts resolving: what "its controller" and "its power"
    # read after the object has left.
    lki: dict = field(default_factory=dict)
    # A triggered ability: the player its event was about ("that player").
    event_player: int | None = None

    def ask(self, state: GameState, seat: int, kind: str, prompt: str, options: list[ChoiceOption]) -> ChoiceOption:
        if len(options) == 1 or not state.player(seat).alive:
            return options[0]
        key = self.choose(state, seat, kind, prompt, options)
        return next((o for o in options if o.key == key), options[0])


_STATS = {"POWER_OF_IT": "power", "TOUGHNESS_OF_IT": "toughness", "MV_OF_IT": "mv"}


def _amount(op: Op, ctx: EffectContext, index: int = 0) -> int:
    if op.amount == "X":
        return ctx.x
    if op.amount in _STATS:
        return max(0, ctx.lki.get(f"{index}:{op.of}", {}).get(_STATS[op.amount], 0))
    return int(op.amount or 0)


def _refs(state: GameState, ability: Ability, op: Op, ctx: EffectContext, index: int) -> list[str]:
    target = ability.target(op.target)
    if target is None:
        return []
    if target.targeted:
        ref = ctx.targets.get(f"{index}:{target.key}")
        return [ref] if ref is not None else []
    if target.selector == "CONTROLLER_OF_IT":
        info = ctx.lki.get(f"{index}:{target.of}")
        return [player_ref(info["controller"])] if info and state.player(info["controller"]).alive else []
    if target.selector == "THAT_PLAYER":
        seat = ctx.event_player
        return [player_ref(seat)] if seat is not None and state.player(seat).alive else []
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
        queue_event_triggers(state, Trigger.OPPONENT_DRAWS, lambda perm, ability: perm.controller != seat, seat)
        queue_event_triggers(state, Trigger.YOU_DRAW, lambda perm, ability: perm.controller == seat)
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
    state: GameState, ref: str, amount: int, *, source_seat: int, source_name: str, deathtouch=False, combat=False, commander=False, commander_id=None
) -> list[Event]:
    """Damage to a player (life loss) or a creature (marked damage)."""
    if amount <= 0:
        return []
    kind, value = parse_ref(ref)
    if kind == "p":
        player = state.player(value)
        player.life -= amount
        if commander and commander_id is not None:
            player.commander_damage[commander_id] = player.commander_damage.get(commander_id, 0) + amount
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
    if amount > 0:
        queue_event_triggers(state, Trigger.GAIN_LIFE, lambda perm, ability: perm.controller == seat)
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
    amount = _amount(op, ctx, index)
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
        case "SEARCH_LIBRARY":
            events += _search(state, op, ctx, amount)
        case "DISCARD_CHOSEN":
            for ref in refs:
                events += _discard_chosen(state, op, ctx, parse_ref(ref)[1])
        case "LOOK_AT_HAND":
            for ref in refs:
                seen = state.player(parse_ref(ref)[1])
                names = ", ".join(c.name for c in seen.hand) or "nothing"
                events.append(
                    Event(EventKind.EFFECT, ctx.controller, f"{state.player(ctx.controller).name} looks at {seen.name}'s hand: {names}.", {"effect": "look_at_hand", "cards": [c.name for c in seen.hand]})
                )
        case "DIG":
            for ref in refs:
                events += _dig(state, op, ctx, parse_ref(ref)[1], amount)
        case "PUT_BACK":
            for ref in refs:
                events += _put_back(state, ctx, parse_ref(ref)[1], amount)
        case "ARRANGE_TOP":
            for ref in refs:
                events += _arrange_top(state, op, ctx, parse_ref(ref)[1], amount)
        case "PAY_MANA":
            from .mana import pay
            from .manacost import parse_mana_cost

            events += pay(state, ctx.controller, parse_mana_cost(op.mana))
        case "COUNTER_UNLESS_PAYS":
            for ref in refs:
                item = state.stack_item(parse_ref(ref)[1])
                if item is not None:
                    events += _counter_unless_pays(state, item, op, ctx)
        case "CREATE_ARTIFACT_TOKEN":
            spec = ARTIFACT_TOKENS[op.token]
            for ref in refs:
                seat = parse_ref(ref)[1]
                for _ in range(amount):
                    enter_battlefield(state, None, seat, spec)
                events.append(
                    Event(EventKind.EFFECT, seat, f"{state.player(seat).name} creates {amount} {spec.name} token{'s' if amount > 1 else ''}.", {"effect": "create_token", "amount": amount, "token": spec.name})
                )
        case "SCRY" | "SURVEIL":
            for ref in refs:
                events += _look_at_top(state, op, ctx, parse_ref(ref)[1], amount)
        case "COUNTER_SPELL":
            for ref in refs:
                item = state.stack_item(parse_ref(ref)[1])
                if item is not None:
                    events += counter_item(state, item, f"countered by {name}", _COUNTERED_ZONES[op.to or "GRAVEYARD"])
    return events


# -- library searches, scry and surveil -----------------------------------------------


def matches_filter(spec: CardSpec, filt: str) -> bool:
    if filt == "CARD":
        return True
    if filt == "LAND":
        return spec.is_land
    if filt == "BASIC_LAND":
        return spec.is_land and "basic" in spec.supertypes
    if filt.startswith("TYPES:"):
        return spec.is_land and bool(set(filt.removeprefix("TYPES:").split("|")) & set(spec.subtypes))
    if filt == "INSTANT_OR_SORCERY":
        return spec.is_spell_only
    if filt == "NONLAND":
        return not spec.is_land
    if filt == "NONCREATURE_NONLAND":
        return not spec.is_land and not spec.is_creature
    if filt == "ARTIFACT_OR_ENCHANTMENT":
        return "artifact" in spec.types or "enchantment" in spec.types
    if filt == "EQUIPMENT":
        return "equipment" in spec.subtypes
    return filt.lower() in spec.types


def _needed_colors(state: GameState, seat: int) -> set[str]:
    """Colors the seat's hand asks for that its lands do not make yet."""
    made = {c for p in state.battlefield if p.controller == seat for a in p.spec.mana_abilities for c in a.colors}
    wanted = {c for card in state.player(seat).hand for pip in card.spec.cost.pips for c in pip}
    return wanted - made


def _land_count(state: GameState, seat: int) -> int:
    in_play = sum(1 for p in state.battlefield if p.controller == seat and p.spec.is_land)
    return in_play + sum(1 for c in state.player(seat).hand if c.spec.is_land)


def card_value(state: GameState, seat: int, spec: CardSpec) -> int:
    """The kernel's estimate of a card for ``seat`` right now (a fact for agents, never binding)."""
    if spec.is_land:
        needed = _needed_colors(state, seat)
        return 2 + 5 * len(needed & {c for a in spec.mana_abilities for c in a.colors}) + (4 if _land_count(state, seat) < 4 else 0)
    castable_soon = spec.mana_value <= _land_count(state, seat) + 1
    return spec.mana_value * 2 + (3 if castable_soon else 0) + (1 if spec.is_creature else 0) - (20 if not spec.supported else 0)


def _search(state: GameState, op: Op, ctx: EffectContext, amount: int) -> list[Event]:
    player = state.player(ctx.controller)
    found = []
    for _ in range(amount):
        seen, options = set(), []
        limit = ctx.x if op.max_mv == "X" else op.max_mv
        for card in player.library:
            fits = matches_filter(card.spec, op.filter) and (limit is None or card.spec.mana_value <= limit)
            fits = fits and (op.max_toughness is None or (card.spec.is_creature and (card.spec.toughness or 0) <= op.max_toughness))
            if fits and card.name not in seen:
                seen.add(card.name)
                options.append(ChoiceOption(f"c{len(options):02d}", f"Find {card.name}", card_value(state, ctx.controller, card.spec), card.id))
        # The library is hidden: a player may always fail to find.
        options.append(ChoiceOption(f"c{len(options):02d}", "Find nothing", -1))
        picked = ctx.ask(state, ctx.controller, "search", f"{ctx.source_name}: search your library", options)
        if picked.card_id is None:
            break
        card = next(c for c in player.library if c.id == picked.card_id)
        player.library.remove(card)
        found.append(card)
    state.rng.shuffle(player.library)
    events = [Event(EventKind.EFFECT, ctx.controller, f"{player.name} searches their library" + (f" and finds {', '.join(c.name for c in found)}." if found else " and finds nothing."), {"effect": "search", "found": [c.name for c in found]})]
    for card in found:
        if op.to == "HAND":
            player.hand.append(card)
        elif op.to == "TOP":
            player.library.insert(0, card)
        elif op.to == "GRAVEYARD":
            put_card(state, card, Zone.GRAVEYARD)
        else:
            perm = enter_battlefield(state, card, ctx.controller)
            if op.to == "BATTLEFIELD_TAPPED":
                perm.tapped = True
    return events


def _look_at_top(state: GameState, op: Op, ctx: EffectContext, seat: int, amount: int) -> list[Event]:
    player = state.player(seat)
    top = player.library[:amount]
    del player.library[:amount]
    away = "graveyard" if op.op == "SURVEIL" else "bottom"
    kept, sent = [], []
    for card in top:
        value = card_value(state, seat, card.spec)
        keep = value >= 5 if not card.spec.is_land else _land_count(state, seat) < 5
        options = [
            ChoiceOption("c00", f"Keep {card.name} on top", 1 if keep else 0, card.id),
            ChoiceOption("c01", f"Put {card.name} {'into the graveyard' if away == 'graveyard' else 'on the bottom'}", 0 if keep else 1, card.id),
        ]
        picked = ctx.ask(state, seat, op.op.lower(), f"{ctx.source_name}: {op.op.lower()}", options)
        (kept if picked.key == "c00" else sent).append(card)
    player.library[0:0] = kept
    for card in sent:
        if away == "graveyard":
            put_card(state, card, Zone.GRAVEYARD)
        else:
            player.library.append(card)
    return [
        Event(
            EventKind.EFFECT,
            seat,
            f"{player.name} {op.op.lower()}s {len(top)}: {len(kept)} on top, {len(sent)} {'to the graveyard' if away == 'graveyard' else 'to the bottom'}.",
            {"effect": op.op.lower(), "kept": len(kept), "away": len(sent)},
        )
    ]


def _discard_chosen(state: GameState, op: Op, ctx: EffectContext, seat: int) -> list[Event]:
    player = state.player(seat)
    revealed = ", ".join(c.name for c in player.hand) or "nothing"
    events = [Event(EventKind.EFFECT, seat, f"{player.name} reveals their hand: {revealed}.", {"effect": "reveal_hand", "cards": [c.name for c in player.hand]})]
    seen, options = set(), []
    for card in player.hand:
        fits = matches_filter(card.spec, op.filter) and (op.max_mv is None or card.spec.mana_value <= op.max_mv)
        if fits and card.name not in seen:
            seen.add(card.name)
            options.append(ChoiceOption(f"c{len(options):02d}", f"Discard {card.name}", card_value(state, seat, card.spec), card.id))
    if not options:
        return events
    picked = ctx.ask(state, ctx.controller, "discard", f"{ctx.source_name}: choose a card {player.name} discards", options)
    card = next(c for c in player.hand if c.id == picked.card_id)
    player.hand.remove(card)
    put_card(state, card, Zone.GRAVEYARD)
    events.append(Event(EventKind.DISCARD, seat, f"{player.name} discards {card.name}.", {"card": card.name, "effect": "discard_chosen"}))
    return events


def _pick_cards(state: GameState, ctx: EffectContext, seat: int, cards: list, count: int, label: str, kind: str, sign: int = 1) -> list:
    """Asks ``seat`` to pick ``count`` of ``cards``, one at a time; returns them in picking order."""
    picked, left = [], list(cards)
    for _ in range(min(count, len(left))):
        seen, options = set(), []
        for card in left:
            if card.name not in seen:
                seen.add(card.name)
                options.append(ChoiceOption(f"c{len(options):02d}", label.format(card.name), sign * card_value(state, seat, card.spec), card.id))
        choice = ctx.ask(state, seat, kind, f"{ctx.source_name}: {label.format('a card').lower()}", options)
        card = next(c for c in left if c.id == choice.card_id)
        left.remove(card)
        picked.append(card)
    return picked


def _dig(state: GameState, op: Op, ctx: EffectContext, seat: int, amount: int) -> list[Event]:
    player = state.player(seat)
    top = player.library[:amount]
    del player.library[:amount]
    taken = _pick_cards(state, ctx, seat, top, op.count, "Take {}", "dig")
    rest = [c for c in top if c not in taken]
    player.hand += taken
    for card in rest:
        if op.to == "GRAVEYARD":
            put_card(state, card, Zone.GRAVEYARD)
        else:
            player.library.append(card)
    where = "into the graveyard" if op.to == "GRAVEYARD" else "on the bottom"
    return [Event(EventKind.EFFECT, seat, f"{player.name} looks at the top {len(top)}, takes {len(taken)} and puts {len(rest)} {where}.", {"effect": "dig", "taken": len(taken), "rest": len(rest)})]


def _put_back(state: GameState, ctx: EffectContext, seat: int, amount: int) -> list[Event]:
    player = state.player(seat)
    # The least valuable cards go back first; the last one picked ends on top.
    back = _pick_cards(state, ctx, seat, player.hand, amount, "Put back {}", "put_back", sign=-1)
    for card in back:
        player.hand.remove(card)
        player.library.insert(0, card)
    return [Event(EventKind.EFFECT, seat, f"{player.name} puts {len(back)} card{'s' if len(back) != 1 else ''} from their hand on top of their library.", {"effect": "put_back", "amount": len(back)})]


def _arrange_top(state: GameState, op: Op, ctx: EffectContext, seat: int, amount: int) -> list[Event]:
    player = state.player(seat)
    top = player.library[:amount]
    if op.shuffle and top:
        worth = sum(card_value(state, seat, c.spec) for c in top) / len(top)
        options = [ChoiceOption("c00", "Keep them", 1 if worth >= 5 else 0), ChoiceOption("c01", "Shuffle", 0 if worth >= 5 else 1)]
        if ctx.ask(state, seat, "shuffle", f"{ctx.source_name}: keep the top cards or shuffle", options).key == "c01":
            state.rng.shuffle(player.library)
            return [Event(EventKind.EFFECT, seat, f"{player.name} shuffles their library.", {"effect": "shuffle"})]
    del player.library[: len(top)]
    ordered = _pick_cards(state, ctx, seat, top, len(top) - 1, "{} on top", "arrange")
    ordered += [c for c in top if c not in ordered]
    player.library[0:0] = ordered
    return [Event(EventKind.EFFECT, seat, f"{player.name} puts the top {len(top)} back in an order of their choice.", {"effect": "arrange_top", "amount": len(top)})]


def _counter_unless_pays(state: GameState, item: StackItem, op: Op, ctx: EffectContext) -> list[Event]:
    from .mana import can_pay, pay, sources

    from .manacost import parse_mana_cost

    cost = parse_mana_cost(op.mana)
    payer = item.controller
    if state.player(payer).alive and can_pay(sources(state, payer), cost):
        options = [ChoiceOption("c00", f"Pay {op.mana}", 1), ChoiceOption("c01", "Don't pay", 0)]
        if ctx.ask(state, payer, "pay", f"{ctx.source_name}: pay {op.mana} or {item.name} is countered", options).key == "c00":
            paid = pay(state, payer, cost)
            return paid + [Event(EventKind.EFFECT, payer, f"{state.player(payer).name} pays {op.mana} for {item.name}.", {"effect": "pay_unless", "mana": op.mana})]
    return counter_item(state, item, f"countered by {ctx.source_name}", _COUNTERED_ZONES[op.to or "GRAVEYARD"])


def _token_ability(cost: Cost, op: Op, text: str) -> Ability:
    return Ability(Trigger.ACTIVATED, (op,), (Target("t1", "YOU"),), cost, text=text)


def _artifact_token(name: str, **extra) -> CardSpec:
    return CardSpec(name=name, types=("artifact",), subtypes=(name.lower(),), cost=ManaCost(), **extra)


# The predefined artifact tokens (CR 111.10).
ARTIFACT_TOKENS = {
    "TREASURE": _artifact_token(
        "Treasure", produced_mana=tuple("WUBRG"), mana_amount=1, mana_abilities=(ManaAbility("WUBRG", sacrifice=True),)
    ),
    "CLUE": _artifact_token(
        "Clue",
        program=CardProgram((_token_ability(Cost(mana="{2}", sacrifice_self=True), Op("DRAW_CARDS", target="t1", amount=1), "{2}, sacrifice this artifact: draw a card."),)),
    ),
    "FOOD": _artifact_token(
        "Food",
        program=CardProgram((_token_ability(Cost(mana="{2}", tap=True, sacrifice_self=True), Op("GAIN_LIFE", target="t1", amount=3), "{2}, {t}, sacrifice this artifact: you gain 3 life."),)),
    ),
    "BLOOD": _artifact_token(
        "Blood",
        program=CardProgram((_token_ability(Cost(mana="{1}", tap=True, sacrifice_self=True, discard=1), Op("DRAW_CARDS", target="t1", amount=1), "{1}, {t}, discard a card, sacrifice this artifact: draw a card."),)),
    ),
}


def cycling_ability(spec: CardSpec) -> Ability:
    """What cycling ``spec`` puts on the stack: draw a card, or find a land (landcycling)."""
    you = (Target("t1", "YOU"),)
    if spec.landcycling:
        op = Op("SEARCH_LIBRARY", target="t1", amount=1, filter=spec.landcycling, to="HAND")
    else:
        op = Op("DRAW_CARDS", target="t1", amount=1)
    return Ability(Trigger.ACTIVATED, (op,), you, text="cycling")


_COUNTERED_ZONES = {"GRAVEYARD": Zone.GRAVEYARD, "EXILE": Zone.EXILE, "TOP": Zone.LIBRARY_TOP, "HAND": Zone.HAND}


def counter_item(state: GameState, item: StackItem, why: str, zone: str = Zone.GRAVEYARD) -> list[Event]:
    """Removes ``item`` from the stack without effect; a spell goes to its owner's ``zone``."""
    if item.card is not None and item.card.spec.uncounterable:
        return [Event(EventKind.EFFECT, item.controller, f"{item.name} can't be countered.", {"card": item.name, "effect": "uncounterable"})]
    if item in state.stack:
        state.stack.remove(item)
    if item.card is not None:
        put_card(state, item.card, Zone.EXILE if item.flashback else zone)
    return [Event(EventKind.COUNTERED, item.controller, f"{item.name} is {why}.", {"card": item.name, "reason": why})]


def last_known(state: GameState, ref: str) -> dict:
    """What "its controller" and "its power" read for ``ref``, captured now."""
    kind, value = parse_ref(ref)
    if kind == "o":
        perm = state.permanent(value)
        return {"controller": perm.controller, "power": power(state, perm), "toughness": toughness(state, perm), "mv": perm.spec.mana_value}
    if kind == "s":
        item = state.stack_item(value)
        spec = item.card.spec if item.card is not None else None
        return {"controller": item.controller, "power": (spec.power or 0) if spec else 0, "toughness": (spec.toughness or 0) if spec else 0, "mv": spec.mana_value if spec else 0}
    return {"controller": value, "power": 0, "toughness": 0, "mv": 0}


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
    ctx.lki = {slot: last_known(state, ref) for slot, ref in legal.items()}
    events: list[Event] = []
    for index, ability in enumerate(abilities):
        # Whether the last optional op of this ability happened ("if you do").
        done = True
        for op in ability.ops:
            if op.if_done and not done:
                continue
            target = ability.target(op.target)
            follows = [key for key in (target.key if target is not None and target.targeted else "", target.of if target is not None else "", op.of) if key]
            if any(f"{index}:{key}" not in legal for key in follows):
                # That target (or the one "it" refers to) became illegal: this part does nothing.
                done = done and not op.optional
                continue
            if op.optional:
                done = _wants(state, ability, op, ctx)
                if not done:
                    continue
            events += apply_op(state, ability, op, ctx, index)
    return events, False


def _wants(state: GameState, ability: Ability, op: Op, ctx: EffectContext) -> bool:
    """Asks the controller whether an optional op happens; a payment it cannot make does not."""
    if op.op == "PAY_MANA":
        from .mana import can_pay, sources
        from .manacost import parse_mana_cost

        if not can_pay(sources(state, ctx.controller), parse_mana_cost(op.mana)):
            return False
    text = describe_op(op, ability)
    options = [ChoiceOption("c00", text[:1].upper() + text[1:], 1), ChoiceOption("c01", "Don't", 0)]
    return ctx.ask(state, ctx.controller, "may", f"{ctx.source_name}: you may {text}", options).key == "c00"
