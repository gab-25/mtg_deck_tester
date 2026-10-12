"""Loading Scryfall's Oracle Cards bulk file into the ``scryfall_cards`` cache."""

import gzip
import json
import tempfile
from dataclasses import dataclass

import requests
from django.db import reset_queries, transaction
from django.utils.dateparse import parse_datetime

from .models import BulkDataLoad, ScryfallCard
from .scryfall import SCRYFALL_HEADERS
from .text_utils import card_slug, front_face_name

BULK_KIND = "oracle_cards"
BULK_METADATA_URL = "https://api.scryfall.com/bulk-data/oracle-cards"

# Layouts that never appear in a decklist. A token named like a real card
# would otherwise collide with it on the same key.
SKIPPED_LAYOUTS = frozenset(
    {"token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar"}
)

_BATCH_SIZE = 1000
_DOWNLOAD_CHUNK = 1 << 20
_GZIP_MAGIC = b"\x1f\x8b"


@dataclass
class LoadCounts:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def card_key(card: dict) -> str:
    """The cache key :func:`decks.scryfall.fetch_card_data` looks this card up by."""
    return f"card_en_{card_slug(front_face_name(card.get('name') or ''))}"


def fetch_metadata() -> tuple[str, object]:
    """Returns the bulk file's URL (gzipped JSONL) and its ``updated_at`` (datetime)."""
    resp = requests.get(BULK_METADATA_URL, headers=SCRYFALL_HEADERS, timeout=30)
    resp.raise_for_status()
    meta = resp.json()
    return meta["jsonl_download_uri"], parse_datetime(meta["updated_at"])


def last_updated_at():
    """``updated_at`` of the newest loaded file whose date is known, or None."""
    load = (
        BulkDataLoad.objects.filter(kind=BULK_KIND, updated_at__isnull=False)
        .order_by("-updated_at")
        .first()
    )
    return load.updated_at if load is not None else None


def download(url: str, dest) -> None:
    """Streams ``url`` into the open binary file ``dest``."""
    with requests.get(url, headers=SCRYFALL_HEADERS, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
            dest.write(chunk)
    dest.flush()


def _write_batch(batch: dict, counts: LoadCounts) -> None:
    existing = set(
        ScryfallCard.objects.filter(pk__in=batch.keys()).values_list("key", flat=True)
    )
    counts.updated += len(existing)
    counts.inserted += len(batch) - len(existing)
    # Overwrites cached cards too, including {"error": "not_found"} entries
    # for cards released after they were looked up.
    ScryfallCard.objects.bulk_create(
        [ScryfallCard(key=key, data=data) for key, data in batch.items()],
        update_conflicts=True,
        unique_fields=["key"],
        update_fields=["data"],
    )
    # With DEBUG on, Django keeps every query's SQL: a batch is megabytes of JSON.
    reset_queries()


def _iter_cards(f):
    """Yields the cards of an open JSONL file (one card per line), gzipped or not."""
    if f.peek(2)[:2] == _GZIP_MAGIC:
        f = gzip.GzipFile(fileobj=f)
    for line in f:
        if line.strip():
            yield json.loads(line)


def load_file(f, updated_at=None) -> LoadCounts:
    """Loads an open Oracle Cards JSONL file and records the load.

    Runs in one transaction: a truncated or malformed file changes nothing.
    """
    counts = LoadCounts()
    seen = set()
    batch = {}
    with transaction.atomic():
        for card in _iter_cards(f):
            key = card_key(card)
            # A few distinct Oracle cards share a name (Un-set variants); the
            # first one keeps the key, as a batch cannot upsert a row twice.
            if card.get("layout") in SKIPPED_LAYOUTS or key in seen:
                counts.skipped += 1
                continue
            seen.add(key)
            batch[key] = card
            if len(batch) >= _BATCH_SIZE:
                _write_batch(batch, counts)
                batch = {}
        if batch:
            _write_batch(batch, counts)
        BulkDataLoad.objects.create(
            kind=BULK_KIND,
            updated_at=updated_at,
            inserted=counts.inserted,
            updated=counts.updated,
            skipped=counts.skipped,
        )
    return counts


def load_remote(if_newer: bool = False):
    """Downloads and loads the current bulk file.

    Returns ``(counts, updated_at)``, with ``counts`` None when ``if_newer``
    found the stored file up to date.
    """
    url, updated_at = fetch_metadata()
    stored = last_updated_at()
    if if_newer and stored is not None and stored >= updated_at:
        return None, updated_at
    with tempfile.TemporaryFile() as tmp:
        download(url, tmp)
        tmp.seek(0)
        return load_file(tmp, updated_at), updated_at
