"""Deck import: parse the pasted text, fetch every card, enforce the format.

The one place the parse -> Scryfall fetch -> legality check workflow lives.
"""

import logging

from mtg_deck_tester.logging_context import job_log_context

from .cache import DbCardCache
from .formats import FORMATS
from .rules.commander import commander_names, deck_color_identity
from .rules.decklist import parse_decklist_text
from .rules.legality import board_of, check_deck, check_decklist
from .scryfall import fetch_card_data

logger = logging.getLogger(__name__)


def decklist_errors(text: str, fmt: str) -> list:
    """Every rule of ``fmt`` the pasted text breaks that can be settled without Scryfall.

    Cheap and synchronous, so the creation form can reject a deck on the spot;
    the rules that need the real cards are enforced by :func:`import_decklist`.
    """
    entries = parse_decklist_text(text, fmt)
    if not entries:
        return ["No cards could be parsed from the decklist."]
    return check_decklist(entries, fmt)


def import_decklist(text: str, cache, fmt: str, progress=None) -> dict:
    """Fetches the cards of a decklist and checks it against ``fmt``.

    Returns ``{"cards", "commander", "color_identity"}``. Raises ``ValueError``
    listing every problem at once when a card cannot be found or the deck is not
    legal in ``fmt``.
    """
    notify = progress or (lambda _msg: None)
    entries = parse_decklist_text(text, fmt)
    if not entries:
        raise ValueError("No cards could be parsed from the decklist.")

    cards = []
    unresolved = []
    for idx, entry in enumerate(entries):
        notify(f"[{idx + 1}/{len(entries)}] Fetching '{entry['name']}'...")
        data = fetch_card_data(entry["name"], cache)
        if data is None:
            unresolved.append(entry["name"])
            continue
        item = {
            "quantity": entry["quantity"],
            "is_commander": entry["is_commander"],
            "data": data,
        }
        if "board" in entry:
            item["board"] = entry["board"]
        cards.append(item)

    # A card that didn't resolve is missing from the deck, which would surface
    # as a puzzling "99 cards" further down: name the culprits instead.
    if unresolved:
        raise ValueError(
            "These cards could not be found on Scryfall (check their spelling):\n"
            + "\n".join(f"• {name}" for name in unresolved)
        )

    issues = check_deck(cards, fmt)
    if issues:
        raise ValueError(
            f"This is not a legal {FORMATS[fmt].label} deck:\n"
            + "\n".join(f"• {issue}" for issue in issues)
        )

    names = commander_names(cards)
    main = [item for item in cards if board_of(item) == "main"]
    return {
        "cards": cards,
        "commander": " / ".join(names),
        # Without a commander the deck's colors are its main deck's.
        "color_identity": deck_color_identity(main),
    }


def run_import(deck_id) -> None:
    """Background job: imports ``deck_id``'s decklist and stores the outcome."""
    from .models import Deck

    with job_log_context("deck", deck_id):
        deck = Deck.objects.get(pk=deck_id)
        Deck.objects.filter(pk=deck_id).update(status=Deck.Status.PROCESSING)
        try:
            result = import_decklist(
                deck.raw_decklist,
                DbCardCache(),
                deck.format,
                progress=lambda msg: logger.info("%s", msg),
            )
        except Exception as exc:  # noqa: BLE001 - record any failure for the user.
            logger.exception("Deck import failed")
            Deck.objects.filter(pk=deck_id).update(status=Deck.Status.FAILED, error=str(exc))
            return
        Deck.objects.filter(pk=deck_id).update(
            status=Deck.Status.READY, error="", **result
        )
        logger.info("Deck imported (commander: %s)", result["commander"] or "none")
