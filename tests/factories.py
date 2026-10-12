"""Synthetic decks and cards shared by the tests (no network, no Scryfall)."""

from playtest.engine.cards import CardSpec, DeckSpec, Kind

FOREST = CardSpec("Forest", Kind.LAND)


def bear(i=0, mana_value=2, power=2):
    return CardSpec(f"Bear {i}", Kind.CREATURE, mana_value, power, power)


def engine_deck(name="Deck", *, lands=38, commander_power=5, commander_cost=4, library=None):
    """A 100-card deck: lands, 2-mana bears, and a creature commander."""
    if library is None:
        library = [FOREST] * lands + [bear(i) for i in range(99 - lands)]
    commander = CardSpec(f"{name} Commander", Kind.CREATURE, commander_cost, commander_power, commander_power)
    return DeckSpec(name=name, commander=commander, library=tuple(library))


def scryfall_card(
    name, type_line, cmc=0.0, power=None, toughness=None, card_id=None, image_urls=()
):
    """A processed Scryfall card, as a Deck stores it in ``cards``."""
    return {
        "id": card_id or name,
        "name": name,
        "type_line": type_line,
        "cmc": cmc,
        "power": power,
        "toughness": toughness,
        "image_urls": list(image_urls),
        "color_identity": ["G"],
        "legalities": {"commander": "legal", "duel": "legal"},
        "faces": [{"name": name, "type_line": type_line, "rules_text": "", "mana_cost": ""}],
    }


def stored_cards(commander="Bear Lord"):
    """A legal 100-card deck in the stored ``Deck.cards`` shape."""
    cards = [
        {
            "quantity": 1,
            "is_commander": True,
            "data": scryfall_card(commander, "Legendary Creature — Bear", 4.0, "5", "5"),
        },
        {"quantity": 40, "is_commander": False, "data": scryfall_card("Forest", "Basic Land — Forest")},
    ]
    cards += [
        {
            "quantity": 1,
            "is_commander": False,
            "data": scryfall_card(f"Bear {i}", "Creature — Bear", 2.0, "2", "2"),
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
