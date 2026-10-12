"""Tests for the ``load_oracle_cards`` command (Scryfall Oracle Cards bulk data)."""

import gzip
import json
from datetime import datetime, timezone

import pytest
from django.core.management import call_command

from decks.cache import DbCardCache
from decks.models import BulkDataLoad, ScryfallCard
from decks.scryfall import fetch_card_data

pytestmark = pytest.mark.django_db

BOLT = {
    "object": "card",
    "id": "bolt-id",
    "lang": "en",
    "name": "Lightning Bolt",
    "layout": "normal",
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "mana_cost": "{R}",
    "cmc": 1.0,
    "prices": {"eur": "1.50"},
}
SPLIT = {
    "id": "split-id",
    "lang": "en",
    "name": "Fire // Ice",
    "layout": "split",
    "type_line": "Instant // Instant",
    "cmc": 4.0,
    "card_faces": [
        {"name": "Fire", "type_line": "Instant", "oracle_text": "Fire deals 2 damage."},
        {"name": "Ice", "type_line": "Instant", "oracle_text": "Tap target permanent."},
    ],
}
MDFC = {
    "id": "mdfc-id",
    "lang": "en",
    "name": "Sink into Stupor // Soporific Springs",
    "layout": "modal_dfc",
    "type_line": "Instant // Land",
    "cmc": 3.0,
    "card_faces": [
        {"name": "Sink into Stupor", "type_line": "Instant"},
        {"name": "Soporific Springs", "type_line": "Land"},
    ],
}
ADVENTURE = {
    "id": "adv-id",
    "lang": "en",
    "name": "Bonecrusher Giant // Stomp",
    "layout": "adventure",
    "type_line": "Creature — Giant // Instant — Adventure",
    "cmc": 3.0,
    "card_faces": [
        {"name": "Bonecrusher Giant", "type_line": "Creature — Giant"},
        {"name": "Stomp", "type_line": "Instant — Adventure"},
    ],
}
# A token sharing a real card's name: it must not shadow the card.
TOKEN = {
    "id": "token-id",
    "lang": "en",
    "name": "Lightning Bolt",
    "layout": "token",
    "type_line": "Token",
}
# Two different Oracle cards with the same name (Un-set variants): first wins.
VARIANT_A = {"id": "var-a", "lang": "en", "name": "Everythingamajig", "layout": "normal"}
VARIANT_B = {"id": "var-b", "lang": "en", "name": "Everythingamajig", "layout": "normal"}

ALL_CARDS = [BOLT, SPLIT, MDFC, ADVENTURE, TOKEN, VARIANT_A, VARIANT_B]


def _jsonl(cards):
    return "".join(json.dumps(card) + "\n" for card in cards).encode("utf-8")


def _write_bulk(tmp_path, cards):
    path = tmp_path / "oracle-cards.jsonl"
    path.write_bytes(_jsonl(cards))
    return str(path)


class _FakeResponse:
    def __init__(self, payload=None, chunks=()):
        self.status_code = 200
        self._payload = payload
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_scryfall(monkeypatch, cards, updated_at):
    """Serves the bulk-data metadata and the bulk file; returns the list of GET URLs."""
    from decks import oracle_bulk

    calls = []
    body = gzip.compress(_jsonl(cards))

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == oracle_bulk.BULK_METADATA_URL:
            return _FakeResponse(
                payload={
                    "jsonl_download_uri": "https://data.scryfall.io/oracle-cards/x.jsonl.gz",
                    "updated_at": updated_at,
                }
            )
        if url == "https://data.scryfall.io/oracle-cards/x.jsonl.gz":
            return _FakeResponse(chunks=[body[:50], body[50:]])
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(oracle_bulk.requests, "get", fake_get)
    return calls


def _no_api(monkeypatch):
    """Fails the test if the per-card Scryfall API is reached."""
    import decks.scryfall

    def boom(url, **kwargs):
        raise AssertionError(f"unexpected Scryfall call: {url}")

    monkeypatch.setattr(decks.scryfall.requests, "get", boom)


def test_loads_cards_under_the_keys_fetch_card_data_looks_up(tmp_path):
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, ALL_CARDS))

    assert set(ScryfallCard.objects.values_list("key", flat=True)) == {
        "card_en_lightning_bolt",
        "card_en_fire",
        "card_en_sink_into_stupor",
        "card_en_bonecrusher_giant",
        "card_en_everythingamajig",
    }
    assert ScryfallCard.objects.get(pk="card_en_lightning_bolt").data == BOLT


def test_loads_a_gzipped_file(tmp_path):
    path = tmp_path / "oracle-cards.jsonl.gz"
    path.write_bytes(gzip.compress(_jsonl([BOLT, SPLIT])))

    call_command("load_oracle_cards", file=str(path))

    assert ScryfallCard.objects.count() == 2


def test_skips_non_deck_layouts_and_keeps_the_first_of_two_same_named_cards(tmp_path):
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, ALL_CARDS))

    assert ScryfallCard.objects.get(pk="card_en_lightning_bolt").data["id"] == "bolt-id"
    assert ScryfallCard.objects.get(pk="card_en_everythingamajig").data["id"] == "var-a"


def test_records_the_load_with_its_counts(tmp_path):
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, ALL_CARDS))

    load = BulkDataLoad.objects.get()
    assert load.kind == "oracle_cards"
    assert (load.inserted, load.updated, load.skipped) == (5, 0, 2)


def test_second_run_updates_instead_of_duplicating(tmp_path):
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, [BOLT]))
    newer_bolt = {**BOLT, "prices": {"eur": "2.00"}}
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, [newer_bolt]))

    assert ScryfallCard.objects.count() == 1
    assert ScryfallCard.objects.get().data["prices"] == {"eur": "2.00"}
    latest = BulkDataLoad.objects.order_by("-loaded_at").first()
    assert (latest.inserted, latest.updated) == (0, 1)


def test_overwrites_a_cached_not_found(tmp_path):
    ScryfallCard.objects.create(key="card_en_lightning_bolt", data={"error": "not_found"})

    call_command("load_oracle_cards", file=_write_bulk(tmp_path, [BOLT]))

    assert ScryfallCard.objects.get(pk="card_en_lightning_bolt").data == BOLT


def test_after_a_load_every_deck_card_resolves_without_the_api(tmp_path, monkeypatch):
    call_command("load_oracle_cards", file=_write_bulk(tmp_path, ALL_CARDS))
    _no_api(monkeypatch)
    cache = DbCardCache()

    for name in [
        "Lightning Bolt",
        "Fire // Ice",
        "Fire",
        "Sink into Stupor // Soporific Springs",
        "Bonecrusher Giant",
    ]:
        assert fetch_card_data(name, cache) is not None, name


def test_a_card_missing_from_the_load_still_goes_through_the_api(tmp_path, monkeypatch):
    import decks.scryfall

    call_command("load_oracle_cards", file=_write_bulk(tmp_path, [BOLT]))
    monkeypatch.setattr(decks.scryfall, "_REQUEST_DELAY", 0)
    requested = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "new", "lang": "en", "name": "Brand New Card"}

    def fake_get(url, **kwargs):
        requested.append(url)
        return _Resp()

    monkeypatch.setattr(decks.scryfall.requests, "get", fake_get)

    assert fetch_card_data("Brand New Card", DbCardCache())["name"] == "Brand New Card"
    assert requested == ["https://api.scryfall.com/cards/named?exact=Brand%20New%20Card"]


def test_downloads_the_file_named_by_the_bulk_metadata(monkeypatch):
    calls = _fake_scryfall(monkeypatch, [BOLT], "2026-10-12T09:00:00+00:00")

    call_command("load_oracle_cards")

    assert ScryfallCard.objects.get().data == BOLT
    assert BulkDataLoad.objects.get().updated_at == datetime(
        2026, 10, 12, 9, 0, tzinfo=timezone.utc
    )
    assert len(calls) == 2


def test_if_newer_skips_when_the_stored_file_is_up_to_date(monkeypatch):
    BulkDataLoad.objects.create(
        kind="oracle_cards", updated_at=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    )
    calls = _fake_scryfall(monkeypatch, [BOLT], "2026-10-12T09:00:00+00:00")

    call_command("load_oracle_cards", if_newer=True)

    assert ScryfallCard.objects.count() == 0
    assert len(calls) == 1  # metadata only, no download


def test_if_newer_loads_when_the_remote_file_is_newer(monkeypatch):
    BulkDataLoad.objects.create(
        kind="oracle_cards", updated_at=datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    )
    _fake_scryfall(monkeypatch, [BOLT], "2026-10-12T09:00:00+00:00")

    call_command("load_oracle_cards", if_newer=True)

    assert ScryfallCard.objects.count() == 1
    assert BulkDataLoad.objects.count() == 2


def test_a_broken_file_leaves_the_table_untouched(tmp_path):
    ScryfallCard.objects.create(key="card_en_lightning_bolt", data=BOLT)
    path = tmp_path / "broken.jsonl"
    path.write_bytes(_jsonl([SPLIT, MDFC])[:-30])

    with pytest.raises(Exception):
        call_command("load_oracle_cards", file=str(path))

    assert list(ScryfallCard.objects.values_list("key", flat=True)) == [
        "card_en_lightning_bolt"
    ]
    assert BulkDataLoad.objects.count() == 0
