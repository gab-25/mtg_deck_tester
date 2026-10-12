"""Syncing one format's metagame decks: fetch, import, compile, replace."""

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from ..importer import import_decklist
from ..models import MetagameDeck
from ..rules.legality import board_of
from . import source_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncReport:
    format: str
    imported: int
    failed: int
    # Mean coverage percent of the imported decks, or None when none was measured.
    mean_coverage: int | None


def _default_coverage(cache):
    def compile_coverage(cards: list) -> dict:
        # The playtest app compiles cards; imported here so decks keeps no
        # module-level dependency on it.
        from playtest.engine.coverage import coverage_report
        from playtest.judge import compile_deck

        return coverage_report([entry.coverage() for entry in compile_deck(cards, cache)])

    return compile_coverage


def sync_format(fmt: str, limit: int, cache, *, fetch=None, compile_coverage=None) -> SyncReport:
    """Replaces ``fmt``'s metagame decks with the current top ``limit`` decks.

    The source is read first: if it fails (:class:`~decks.metagame.SourceError`),
    nothing is changed. A deck that cannot be imported is kept as ``failed`` with
    the reason; the others are compiled and their coverage stored.
    """
    source = source_for(fmt)
    fetch = fetch or source.fetch
    compile_coverage = compile_coverage or _default_coverage(cache)
    entries = fetch(fmt, limit)

    fetched_at = timezone.now()
    decks = []
    for entry in entries:
        deck = MetagameDeck(
            format=fmt,
            source=source.SOURCE,
            archetype=entry.archetype[:255],
            source_url=entry.source_url[:500],
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
            main = [item for item in deck.cards if board_of(item) == "main"]
            deck.coverage = compile_coverage(main)
        decks.append(deck)

    with transaction.atomic():
        MetagameDeck.objects.filter(format=fmt).delete()
        MetagameDeck.objects.bulk_create(decks)

    ready = [d for d in decks if d.status == MetagameDeck.Status.READY]
    measured = [d.coverage_percent for d in ready if d.coverage_percent is not None]
    return SyncReport(
        format=fmt,
        imported=len(ready),
        failed=len(decks) - len(ready),
        mean_coverage=sum(measured) // len(measured) if measured else None,
    )
