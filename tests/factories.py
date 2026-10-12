"""Synthetic decks and cards shared by the tests (no network, no Scryfall)."""

from playtest.engine.cards import DeckSpec, card

FOREST = card("Forest", "Basic Land", produces="G")


def bear(i=0, cost="{1}{G}", power=2, keywords=()):
    return card(f"Bear {i}", "Creature", cost, power, power, keywords=keywords)


def engine_deck(name="Deck", *, lands=38, commander_power=5, commander_cost="{3}{G}", library=None):
    """A 100-card deck: Forests, green 2/2s, and a creature commander."""
    if library is None:
        library = [FOREST] * lands + [bear(i) for i in range(99 - lands)]
    commander = card(f"{name} Commander", "Legendary Creature", commander_cost, commander_power, commander_power)
    return DeckSpec(name=name, commander=commander, library=tuple(library))


def _cost(cmc):
    """A green mana cost of mana value ``cmc``: 4 -> ``{3}{G}``."""
    cmc = int(cmc)
    if not cmc:
        return ""
    return (f"{{{cmc - 1}}}" if cmc > 1 else "") + "{G}"


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
        "faces": [{"name": name, "type_line": type_line, "rules_text": "", "mana_cost": _cost(cmc)}],
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


def cache_raw_cards(cards):
    """Caches a raw Scryfall card for each stored card, as a real import does."""
    from decks.cache import DbCardCache
    from decks.text_utils import card_slug, front_face_name
    from playtest.judge import raw_from_stored

    cache = DbCardCache()
    for item in cards:
        data = item["data"]
        cache.set_card(f"card_en_{card_slug(front_face_name(data['name']))}", raw_from_stored(data))


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
    cache_raw_cards(defaults["cards"])
    return Deck.objects.create(owner=owner, name=name, format=fmt, **defaults)


def legal_decklist(commander="Atraxa, Praetors' Voice"):
    """A 100-card singleton Commander decklist the creation form accepts."""
    lines = ["Commander", f"1 {commander}", "", "Deck"]
    lines += [f"1 Spell {i}" for i in range(60)]
    lines.append("39 Forest")
    return "\n".join(lines)


def legal_constructed_decklist():
    """A legal 60+15 Modern decklist."""
    lines = [f"4 Spell {i}" for i in range(9)] + ["24 Mountain", "", "Sideboard"]
    lines += [f"3 Side {i}" for i in range(5)]
    return "\n".join(lines)


def constructed_cards(fmt="modern"):
    """A legal 60+15 deck in the stored ``Deck.cards`` shape."""
    cards = [
        {
            "quantity": 4,
            "is_commander": False,
            "board": "main",
            "data": scryfall_card(f"Spell {i}", "Creature — Goblin", 1.0, "2", "1"),
        }
        for i in range(9)
    ]
    cards.append(
        {"quantity": 24, "is_commander": False, "board": "main", "data": scryfall_card("Mountain", "Basic Land — Mountain")}
    )
    cards += [
        {"quantity": 3, "is_commander": False, "board": "side", "data": scryfall_card(f"Side {i}", "Instant", 1.0)}
        for i in range(5)
    ]
    for item in cards:
        item["data"]["legalities"][fmt] = "legal"
        item["data"]["color_identity"] = ["R"]
    return cards


def make_constructed_deck(owner, name="Goblins", fmt="modern"):
    return make_deck(owner, name=name, fmt=fmt, cards=constructed_cards(fmt), commander="", color_identity=["R"])
