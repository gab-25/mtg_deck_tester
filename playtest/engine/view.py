"""What one seat is allowed to know about the game."""

from .actions import commander_cost
from .cards import cost_text
from .mana import available_mana, mana_sources
from .state import Card, GameState, Permanent


def card_dict(card: Card) -> dict:
    """A card in a hidden-to-others or public zone (hand, command zone)."""
    spec = card.spec
    data = {
        "id": card.id,
        "name": card.name,
        "kind": spec.kind,
        "mana_cost": cost_text(spec.mana_cost),
        "mana_value": spec.mana_value,
    }
    if spec.colors:
        data["colors"] = list(spec.colors)
    if spec.kind == "creature":
        data["power"] = spec.power
        data["toughness"] = spec.toughness
    if spec.keywords:
        data["keywords"] = list(spec.keywords)
    if spec.produced_mana:
        data["produces"] = list(spec.produced_mana)
    if spec.instant_speed:
        data["instant_speed"] = True
    if card.is_commander:
        data["commander"] = True
    return data


def permanent_dict(perm: Permanent) -> dict:
    spec = perm.spec
    data = {"id": perm.id, "card_id": perm.card.id, "name": perm.name, "kind": spec.kind, "mana_value": spec.mana_value}
    if perm.is_creature:
        data["power"] = perm.power
        data["toughness"] = perm.toughness
        if perm.damage:
            data["damage"] = perm.damage
        if perm.sick and not perm.has("haste"):
            data["summoning_sick"] = True
    if spec.keywords:
        data["keywords"] = list(spec.keywords)
    if spec.produced_mana:
        data["produces"] = list(spec.produced_mana)
    if perm.tapped:
        data["tapped"] = True
    if perm.attacking is not None:
        data["attacking_seat"] = perm.attacking
    if perm.blocking is not None:
        data["blocking_id"] = perm.blocking
    if perm.card.is_commander:
        data["commander"] = True
    return data


def _permanents(player) -> list[dict]:
    return [permanent_dict(p) for p in player.battlefield]


def _commander_damage_from(state: GameState, victim, seat: int) -> int:
    """Combat damage ``victim`` took from the commanders ``seat`` owns."""
    return sum(n for cid, n in victim.commander_damage.items() if state.commanders.get(cid) == seat)


def player_view(state: GameState, seat: int) -> dict:
    """The game from ``seat``'s side of the table, as plain JSON-ready data.

    Hidden information stays hidden: opponents' hands and every library are
    shown as counts only.
    """
    me = state.player(seat)
    limit = state.rules.commander_damage_limit
    return {
        "round": state.round,
        "turn": state.turn,
        "step": str(state.step) if state.step else "",
        "active_seat": state.active_seat,
        "your_turn": state.active_seat == seat,
        "commander_damage_limit": limit,
        "stack": [
            {"id": item.id, "name": item.card.name, "controller_seat": item.controller, "x": item.x}
            for item in state.stack
        ],
        "you": {
            "seat": seat,
            "deck": me.deck_name,
            "life": me.life,
            "hand": [card_dict(c) for c in me.hand],
            "battlefield": _permanents(me),
            "command_zone": [card_dict(c) for c in me.command],
            "commander_cost": commander_cost(state, seat),
            "graveyard": [c.name for c in me.graveyard],
            "library_count": len(me.library),
            "lands_played_this_turn": me.lands_played,
            "mana_available": available_mana(mana_sources(me)),
            "commander_damage_taken": {
                str(p.seat): _commander_damage_from(state, me, p.seat) for p in state.opponents(seat)
            },
        },
        "opponents": [
            {
                "seat": p.seat,
                "deck": p.deck_name,
                "life": p.life,
                "hand_count": len(p.hand),
                "library_count": len(p.library),
                "battlefield": _permanents(p),
                "command_zone": [card_dict(c) for c in p.command],
                "graveyard": [c.name for c in p.graveyard],
                "commander_damage_taken_from_you": _commander_damage_from(state, p, seat),
            }
            for p in state.opponents(seat)
        ],
    }
