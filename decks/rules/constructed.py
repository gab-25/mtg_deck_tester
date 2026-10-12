"""Constructed formats (Standard … Pauper): deck-construction rules.

A main deck of at least 60 cards, a sideboard of at most 15, at most four
copies of a card across both (basic lands and "any number" cards aside), and
the format's ban list. In Vintage a restricted card is limited to one copy.
Like :mod:`.commander`, the rules run twice: on the parsed names, and on the
fetched cards, reporting every problem at once.
"""

from ..formats import FORMATS
from ..text_utils import front_face_name
from .commander import ANY_NUMBER_CARD_NAMES, BASIC_LAND_NAMES, _allows_duplicates, _tally


def board_of(item: dict) -> str:
    """The zone of a deck entry: ``"main"`` unless it says ``"side"``."""
    return "side" if item.get("board") == "side" else "main"


def _size_issues(main: int, side: int, fmt) -> list:
    issues = []
    if main < fmt.deck_size:
        issues.append(f"The main deck has {main} cards; {fmt.label} needs at least {fmt.deck_size}.")
    if side > fmt.sideboard_size:
        issues.append(f"The sideboard has {side} cards; {fmt.label} allows at most {fmt.sideboard_size}.")
    return issues


def _commander_issue(cards, fmt) -> list:
    if any(item.get("is_commander") for item in cards):
        return [f"{fmt.label} has no commander: remove the Commander header or *CMDR* tag."]
    return []


def _copies_issue(name: str, qty: int, limit: int, why: str = "") -> list:
    return [f"{qty} copies of “{name}”: at most {limit}{why}, main deck and sideboard together."]


def _board_totals(cards) -> tuple[int, int]:
    main = sum(item["quantity"] for item in cards if board_of(item) == "main")
    side = sum(item["quantity"] for item in cards if board_of(item) == "side")
    return main, side


def check_decklist(entries: list, fmt: str) -> list:
    """Every rule of ``fmt`` the parsed decklist breaks (names only)."""
    rules = FORMATS[fmt]
    issues = _size_issues(*_board_totals(entries), rules)
    issues += _commander_issue(entries, rules)
    totals = _tally((front_face_name(e["name"]).lower(), e["name"], e["quantity"]) for e in entries)
    for key, (name, qty) in totals.items():
        if qty > rules.max_copies and key not in BASIC_LAND_NAMES | ANY_NUMBER_CARD_NAMES:
            issues += _copies_issue(name, qty, rules.max_copies)
    return issues


def check_deck(processed_cards: list, fmt: str) -> list:
    """Every rule of ``fmt`` the fetched deck breaks, the ban list included."""
    rules = FORMATS[fmt]
    issues = _size_issues(*_board_totals(processed_cards), rules)
    issues += _commander_issue(processed_cards, rules)
    # The Scryfall id is the same for both spellings of a double-faced card.
    totals = _tally(
        (item["data"].get("id") or item["data"].get("name", ""), item["data"], item["quantity"])
        for item in processed_cards
    )
    for card, qty in totals.values():
        legality = (card.get("legalities") or {}).get(rules.legality_key)
        name = card.get("name", "Unknown card")
        if legality == "restricted" and qty > 1:
            issues += _copies_issue(name, qty, 1, f" (restricted in {rules.label})")
        elif qty > rules.max_copies and not _allows_duplicates(card):
            issues += _copies_issue(name, qty, rules.max_copies)
        if legality == "banned":
            issues.append(f"“{name}” is banned in {rules.label}.")
        elif legality == "not_legal":
            issues.append(f"“{name}” is not legal in {rules.label}.")
    return issues
