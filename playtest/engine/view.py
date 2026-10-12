"""What one seat is allowed to know about the game.

Hidden information stays hidden: opponents' hands and every library are
counts only. Cards show what they *do* as a short summary built from their
compiled program, never their oracle text, and permanents show their current
power, toughness and keywords (static abilities included).
"""

from .actions import commander_cost
from .dsl import describe_ability
from .effects import keywords, power, ref_label, target_slots, toughness
from .mana import available_colors, available_mana, sources
from .state import Card, GameState, Permanent


def effects(spec) -> list[dict]:
    """A card's abilities, structured (for code) and rendered (for models)."""
    if spec.program is None:
        return []
    out = []
    for ability in spec.program.abilities:
        ops = []
        for op in ability.ops:
            target = ability.target(op.target)
            entry = {"op": op.op}
            if target is not None:
                entry["selector"] = target.selector
            for field in ("amount", "power", "toughness", "keyword", "counter", "duration"):
                value = getattr(op, field)
                if value not in (None, ""):
                    entry[field] = value
            ops.append(entry)
        out.append({"trigger": ability.trigger, "text": describe_ability(ability), "ops": ops, "mode": ability.mode_group})
    return out


def card_view(card: Card) -> dict:
    spec = card.spec
    data = {
        "id": card.id,
        "name": spec.name,
        "types": list(spec.types),
        "cost": str(spec.cost) if not spec.is_land else "",
        "mana_value": spec.mana_value,
    }
    if spec.is_creature:
        data["power"], data["toughness"] = spec.power, spec.toughness
    if spec.keywords:
        data["keywords"] = list(spec.keywords)
    if spec.is_land or spec.is_mana_source:
        data["makes_mana"] = "".join(spec.produced_mana)
    if not spec.supported:
        data["unsupported"] = True
    elif spec.program.abilities:
        data["effects"] = effects(spec)
    if card.is_commander:
        data["commander"] = True
    return data


def permanent_view(state: GameState, perm: Permanent) -> dict:
    spec = perm.spec
    data = {"id": perm.id, "name": perm.name, "controller": perm.controller, "types": list(spec.types)}
    if spec.is_creature:
        data["power"], data["toughness"] = power(state, perm), toughness(state, perm)
        if perm.damage:
            data["damage"] = perm.damage
        if perm.sick and "haste" not in keywords(state, perm):
            data["summoning_sick"] = True
    kws = sorted(keywords(state, perm))
    if kws:
        data["keywords"] = kws
    if perm.tapped:
        data["tapped"] = True
    if perm.plus_counters or perm.minus_counters:
        data["counters"] = {"+1/+1": perm.plus_counters, "-1/-1": perm.minus_counters}
    if perm.attacking is not None:
        data["attacking"] = perm.attacking
    if perm.blocking is not None:
        data["blocking"] = perm.blocking
    if perm.blocked_by:
        data["blocked_by"] = list(perm.blocked_by)
    if perm.is_token:
        data["token"] = True
    if perm.is_commander:
        data["commander"] = True
    if spec.is_mana_source:
        data["makes_mana"] = "".join(spec.produced_mana)
    if not spec.supported:
        data["unsupported"] = True
    elif spec.program.abilities:
        data["effects"] = effects(spec)
    return data


def player_view(state: GameState, seat: int) -> dict:
    """The game from ``seat``'s side of the table, as plain JSON-ready data."""
    me = state.player(seat)
    srcs = sources(state, seat)
    costs = [str(commander_cost(state, seat, c.id)) for c in sorted(me.command, key=lambda c: c.id)]
    battlefield = sorted(state.battlefield, key=lambda p: p.id)
    return {
        "round": state.round,
        "turn": state.turn,
        "step": str(state.step),
        "format": state.rules.format_name,
        "active_seat": state.active_seat,
        "you": {
            "seat": seat,
            "deck": me.name,
            "life": me.life,
            "mana_available": available_mana(srcs),
            "mana_colors": "".join(c for c in "WUBRGC" if c in available_colors(srcs)),
            "hand": [card_view(c) for c in me.hand],
            "battlefield": [permanent_view(state, p) for p in battlefield if p.controller == seat],
            "command_zone": [card_view(c) for c in me.command],
            "commander_cost": costs or None,
            "graveyard": [c.name for c in me.graveyard],
            "library_count": len(me.library),
            "lands_played_this_turn": me.lands_played,
            "commander_damage_taken": {
                state.commanders[k][1]: v for k, v in sorted(me.commander_damage.items())
            },
        },
        "opponents": [
            {
                "seat": p.seat,
                "deck": p.name,
                "life": p.life,
                "hand_count": len(p.hand),
                "library_count": len(p.library),
                "battlefield": [permanent_view(state, perm) for perm in battlefield if perm.controller == p.seat],
                "command_zone": [card_view(c) for c in p.command],
                "commander_damage_taken_from_you": sum(
                    v for k, v in p.commander_damage.items() if state.commanders[k][0] == seat
                ),
            }
            for p in (state.player(s) for s in state.turn_order_from(seat) if s != seat)
        ],
        "choosing_target": _choosing_target(state, seat),
        "choice": _choice(state, seat),
        "stack": [
            {
                "id": item.id,
                "name": item.name,
                "controller": item.controller,
                "spell": item.is_spell,
                "targets": [ref_label(state, ref) for ref in item.targets.values()],
            }
            for item in state.stack
        ],
    }


def _choosing_target(state: GameState, seat: int) -> dict | None:
    """What the target being chosen is for, when ``seat`` is choosing one."""
    pending = state.pending_target
    item = state.stack_item(pending.item_id) if pending is not None else None
    if item is None or item.controller != seat:
        return None
    index, key = pending.key.split(":")
    ability = item.abilities[int(index)]
    ops = [op for op in ability.ops if op.target == key]
    target = dict(target_slots(item.abilities))[pending.key]
    return {
        "source": item.name,
        "selector": target.selector,
        "ops": [{"op": op.op, "amount": op.amount, "power": op.power, "toughness": op.toughness, "counter": op.counter} for op in ops],
    }


def _choice(state: GameState, seat: int) -> dict | None:
    """The choice ``seat`` is answering, when an effect asked one."""
    pending = state.pending_choice
    if pending is None or pending.seat != seat:
        return None
    return {
        "kind": pending.kind,
        "prompt": pending.prompt,
        "options": [{"key": o.key, "label": o.label, "score": o.score} for o in pending.options],
    }
