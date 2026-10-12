"""View-models: stored deck data shaped for the templates."""

from urllib.parse import urlencode

from .rules.cards import CATEGORY_ORDER, classify_card
from .rules.commander import commanders

# Plural display labels for card categories (section headers on the deck page).
CATEGORY_LABELS = {
    "Creature": "Creatures",
    "Land": "Lands",
    "Planeswalker": "Planeswalkers",
    "Instant": "Instants",
    "Sorcery": "Sorceries",
    "Artifact": "Artifacts",
    "Enchantment": "Enchantments",
    "Battle": "Battles",
    "Other": "Other",
}

# WUBRG pip colors and per-category accent colors.
COLOR_HEX = {
    "W": "#f3ecd2",
    "U": "#4b8fd6",
    "B": "#7c6f86",
    "R": "#d05a3e",
    "G": "#4c9e6a",
    "C": "#b7b0a8",
}
TYPE_HEX = {
    "Creature": "#8fd08f",
    "Instant": "#7fb6e0",
    "Sorcery": "#c79be0",
    "Land": "#d6c08a",
    "Artifact": "#b7b0a8",
    "Enchantment": "#e0a8c8",
    "Planeswalker": "#e8b64c",
    "Battle": "#d05a3e",
    "Other": "#b7b0a8",
}


def image_urls(card_data: dict) -> list:
    """A stored card's face images, on Scryfall's CDN."""
    return card_data.get("image_urls", [])


def image_query(card_data: dict) -> str:
    """``url=…&url=…`` listing every face, for the zoom modal's ``hx-get``.

    Empty when the card has no image, which the template uses as its
    "no modal to open" guard.
    """
    return urlencode([("url", url) for url in image_urls(card_data)])


def deck_pips(deck) -> list:
    """The deck's color identity as pip view-models (colorless when empty)."""
    letters = deck.color_identity or ["C"]
    return [{"letter": c, "hex": COLOR_HEX[c]} for c in letters]


def card_groups(stored_cards: list) -> list:
    """Groups the main deck's cards by category into row view-models for the deck page."""
    stored_cards = [item for item in stored_cards if item.get("board") != "side"]
    grouped = {cat: [] for cat in CATEGORY_ORDER}
    for item in stored_cards:
        grouped[classify_card(item["data"])].append(item)

    groups = []
    for cat in CATEGORY_ORDER:
        items = grouped[cat]
        if not items:
            continue
        rows = []
        for item in items:
            data = item["data"]
            urls = image_urls(data)
            rows.append(
                {
                    "name": data.get("name", ""),
                    "type": cat,
                    "type_hex": TYPE_HEX[cat],
                    "mv": int(data.get("cmc", 0) or 0),
                    "oracle": "\n".join(
                        f["rules_text"] for f in data.get("faces", []) if f.get("rules_text")
                    ),
                    "quantity": item["quantity"],
                    # The row shows the front face; the modal carries them all.
                    "image": urls[0] if urls else "",
                    "image_query": image_query(data),
                    "is_commander": item.get("is_commander", False),
                }
            )
        groups.append(
            {
                "label": CATEGORY_LABELS[cat],
                "hex": TYPE_HEX[cat],
                "count": sum(i["quantity"] for i in items),
                "cards": rows,
            }
        )
    return groups


def sideboard_group(stored_cards: list) -> dict | None:
    """The sideboard as one row group for the deck page, or None when there is none."""
    side = [{**item, "board": "main"} for item in stored_cards if item.get("board") == "side"]
    if not side:
        return None
    rows = [row for group in card_groups(side) for row in group["cards"]]
    return {
        "label": "Sideboard",
        "hex": TYPE_HEX["Other"],
        "count": sum(item["quantity"] for item in side),
        "cards": rows,
    }


def commander_cards(stored_cards: list) -> list:
    """View-models for the commander featured at the top of the deck page."""
    cards = []
    for item in commanders(stored_cards):
        data = item["data"]
        urls = image_urls(data)
        cards.append(
            {
                "name": data.get("name", ""),
                "type_line": data.get("type_line", ""),
                "image": urls[0] if urls else "",
                "image_query": image_query(data),
            }
        )
    return cards
