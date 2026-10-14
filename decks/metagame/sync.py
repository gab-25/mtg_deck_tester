"""Syncing one format's metagame decks: fetch, import, replace."""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

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


def sync_format(fmt: str, limit: int, cache, *, fetch=None) -> SyncReport:
    """Replaces ``fmt``'s metagame decks with the current top ``limit`` decks.

    The source is read first: if it fails (:class:`~decks.metagame.SourceError`),
    or not one of its decks can be imported, nothing is changed. A deck that
    cannot be imported is kept as ``failed`` with the reason. Forge is not
    asked: whether a deck is playable is not checked at sync.
    """
    source = source_for(fmt)
    fetch = fetch or source.fetch
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
        decks.append(deck)

    if decks and not any(d.status == MetagameDeck.Status.READY for d in decks):
        # Every deck failing means the source or Scryfall answered nonsense
        # (an error page, an outage): keep the previous snapshot.
        raise SourceError(f"no deck could be imported for {fmt} ({decks[0].error[:200]})")

    with transaction.atomic():
        MetagameDeck.objects.filter(format=fmt).delete()
        MetagameDeck.objects.bulk_create(decks)

    ready = sum(1 for d in decks if d.status == MetagameDeck.Status.READY)
    return SyncReport(format=fmt, imported=ready, failed=len(decks) - ready)
