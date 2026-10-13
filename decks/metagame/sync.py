"""Syncing one format's metagame decks: fetch, import, compile, replace."""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from ..formats import FORMATS
from ..importer import import_decklist
from ..models import MetagameDeck
from . import SourceError, source_for

logger = logging.getLogger(__name__)


def _web_url(url: str) -> str:
    """Only http(s) links reach the pages: a source could send anything."""
    url = (url or "").strip()
    return url[:500] if url.lower().startswith(("https://", "http://")) else ""


@dataclass(frozen=True)
class SyncReport:
    format: str
    imported: int
    failed: int
    # Imported decks with cards Forge does not know, or None when the Forge
    # bridge could not check them.
    unknown_to_forge: int | None


def forge_check(fmt: str):
    """Checks a deck's cards against the Forge bridge; ``{}`` when it is unreachable."""

    def check(cards: list) -> dict:
        # The playtest app talks to Forge; imported here so decks keeps no
        # module-level dependency on it.
        from playtest import forge

        try:
            result = forge.client().check_deck(
                fmt, forge.decklist_text(cards, has_commander=FORMATS[fmt].has_commander)
            )
        except forge.ForgeError as exc:
            logger.info("Forge could not check a %s metagame deck: %s", fmt, exc)
            return {}
        return {"unresolved": result.get("unresolved", []), "unsupported_cards": result.get("unsupported_cards", [])}

    return check


def sync_format(fmt: str, limit: int, cache, *, fetch=None, check=None) -> SyncReport:
    """Replaces ``fmt``'s metagame decks with the current top ``limit`` decks.

    The source is read first: if it fails (:class:`~decks.metagame.SourceError`),
    or not one of its decks can be imported, nothing is changed. A deck that cannot be imported is kept as ``failed`` with
    the reason; the others are checked against the Forge bridge (``check``),
    which names the cards it does not know.
    """
    source = source_for(fmt)
    fetch = fetch or source.fetch
    check = check or forge_check(fmt)
    entries = fetch(fmt, limit)

    fetched_at = timezone.now()
    decks = []
    for entry in entries:
        deck = MetagameDeck(
            format=fmt,
            source=source.SOURCE,
            archetype=entry.archetype[:255],
            source_url=_web_url(entry.source_url),
            meta_share=entry.meta_share,
            rank=entry.rank,
            fetched_at=fetched_at,
            raw_decklist=entry.decklist,
        )
        try:
            result = import_decklist(entry.decklist, cache, fmt)
        except ValueError as exc:
            deck.status = MetagameDeck.Status.FAILED
            deck.error = str(exc)
            logger.info("Metagame deck %s (%s) failed: %s", entry.archetype, fmt, exc)
        else:
            deck.cards = result["cards"]
            deck.commander = result["commander"]
            deck.color_identity = result["color_identity"]
            deck.coverage = check(deck.cards)
        decks.append(deck)

    if decks and not any(d.status == MetagameDeck.Status.READY for d in decks):
        # Every deck failing means the source or Scryfall answered nonsense
        # (an error page, an outage): keep the previous snapshot.
        raise SourceError(f"no deck could be imported for {fmt} ({decks[0].error[:200]})")

    with transaction.atomic():
        MetagameDeck.objects.filter(format=fmt).delete()
        MetagameDeck.objects.bulk_create(decks)

    ready = [d for d in decks if d.status == MetagameDeck.Status.READY]
    checked = [d for d in ready if d.forge_checked]
    return SyncReport(
        format=fmt,
        imported=len(ready),
        failed=len(decks) - len(ready),
        unknown_to_forge=sum(1 for d in checked if d.unknown_cards) if checked else None,
    )
