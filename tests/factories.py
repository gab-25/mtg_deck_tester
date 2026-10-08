"""Synthetic decks and cards shared by the tests (no network, no Scryfall)."""

from playtest.engine.cards import CardSpec, DeckSpec, Kind
from playtest.engine.manacost import parse_mana_cost

FOREST = CardSpec("Forest", Kind.LAND, produced_mana=("G",))
ISLAND = CardSpec("Island", Kind.LAND, produced_mana=("U",))


def green_cost(mana_value):
    """``{N}{G}``: one green pip, the rest generic."""
    if mana_value <= 0:
        return parse_mana_cost("")
    generic = f"{{{mana_value - 1}}}" if mana_value > 1 else ""
    return parse_mana_cost(generic + "{G}")


def creature(name, power=2, toughness=None, *, mana_value=2, keywords=(), legendary=False, cost=None):
    return CardSpec(
        name,
        Kind.CREATURE,
        mana_value,
        power,
        power if toughness is None else toughness,
        cost=cost or green_cost(mana_value),
        colors=("G",),
        keywords=tuple(keywords),
        legendary=legendary,
        types=("Creature",),
    )


def bear(i=0, mana_value=2, power=2):
    return creature(f"Bear {i}", power, mana_value=mana_value)


def engine_deck(name="Deck", *, lands=38, commander_power=5, commander_cost=4, library=None, commander=None):
    """A 100-card deck: Forests, 2-mana bears, and a creature commander."""
    if library is None:
        library = [FOREST] * lands + [bear(i) for i in range(99 - lands)]
    if commander is None:
        commander = creature(
            f"{name} Commander", commander_power, mana_value=commander_cost, legendary=True
        )
    return DeckSpec(name=name, commander=commander, library=tuple(library))


def scryfall_card(
    name, type_line, cmc=0.0, power=None, toughness=None, card_id=None, mana_cost="", produced_mana=None
):
    """A processed Scryfall card, as a Deck stores it in ``cards``."""
    return {
        "id": card_id or name,
        "name": name,
        "type_line": type_line,
        "cmc": cmc,
        "power": power,
        "toughness": toughness,
        "image_paths": [],
        "color_identity": ["G"],
        "produced_mana": produced_mana or [],
        "legalities": {"commander": "legal", "duel": "legal"},
        "faces": [{"name": name, "type_line": type_line, "rules_text": "", "mana_cost": mana_cost}],
    }


def stored_cards(commander="Bear Lord"):
    """A legal 100-card deck in the stored ``Deck.cards`` shape."""
    cards = [
        {
            "quantity": 1,
            "is_commander": True,
            "data": scryfall_card(commander, "Legendary Creature — Bear", 4.0, "5", "5", mana_cost="{2}{G}{G}"),
        },
        {
            "quantity": 40,
            "is_commander": False,
            "data": scryfall_card("Forest", "Basic Land — Forest", produced_mana=["G"]),
        },
    ]
    cards += [
        {
            "quantity": 1,
            "is_commander": False,
            "data": scryfall_card(f"Bear {i}", "Creature — Bear", 2.0, "2", "2", mana_cost="{1}{G}"),
        }
        for i in range(59)
    ]
    return cards


def make_deck(owner, name="Bears", fmt="commander", **fields):
    from decks.models import Deck

    defaults = {
        "raw_decklist": "",
        "cards": stored_cards(),
        "commander": "Bear Lord",
        "color_identity": ["G"],
        "status": Deck.Status.READY,
    }
    defaults.update(fields)
    return Deck.objects.create(owner=owner, name=name, format=fmt, **defaults)


def legal_decklist(commander="Atraxa, Praetors' Voice"):
    """A 100-card singleton Commander decklist the creation form accepts."""
    lines = ["Commander", f"1 {commander}", "", "Deck"]
    lines += [f"1 Spell {i}" for i in range(60)]
    lines.append("39 Forest")
    return "\n".join(lines)
