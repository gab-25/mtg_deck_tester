"""One entry point for deck legality, whatever the format's family."""

from ..formats import FORMATS
from . import commander, constructed
from .constructed import board_of

__all__ = ["board_of", "check_deck", "check_decklist"]


def check_decklist(entries: list, fmt: str) -> list:
    """Every rule of ``fmt`` the parsed decklist breaks (names only)."""
    if FORMATS[fmt].has_commander:
        return commander.check_decklist(entries)
    return constructed.check_decklist(entries, fmt)


def check_deck(processed_cards: list, fmt: str) -> list:
    """Every rule of ``fmt`` the fetched deck breaks."""
    if FORMATS[fmt].has_commander:
        return commander.check_deck(processed_cards, fmt)
    return constructed.check_deck(processed_cards, fmt)
