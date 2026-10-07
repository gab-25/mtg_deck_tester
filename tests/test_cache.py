"""Tests for the Scryfall cache backends (filesystem and database)."""

import pytest

from decks.cache import DbCardCache, FileCardCache
from decks.scryfall import fetch_card_data


def _exercise_cache(cache):
    # Cards.
    assert cache.get_card("card_en_forest") is None
    cache.set_card("card_en_forest", {"name": "Forest", "lang": "en"})
    assert cache.get_card("card_en_forest") == {"name": "Forest", "lang": "en"}
    # Overwrite.
    cache.set_card("card_en_forest", {"name": "Forest", "lang": "en", "cmc": 0})
    assert cache.get_card("card_en_forest")["cmc"] == 0

    # Images.
    assert cache.has_image("img_x.jpg") is False
    assert cache.get_image("img_x.jpg") is None
    cache.set_image("img_x.jpg", b"\x01\x02\x03")
    assert cache.has_image("img_x.jpg") is True
    assert cache.get_image("img_x.jpg") == b"\x01\x02\x03"


def test_file_card_cache_roundtrip(tmp_path):
    _exercise_cache(FileCardCache(str(tmp_path)))


@pytest.mark.django_db
def test_db_card_cache_roundtrip():
    _exercise_cache(DbCardCache())


@pytest.mark.django_db
def test_fetch_card_data_uses_cache_without_network():
    """A cached English card is processed straight from the cache (no HTTP)."""
    cache = DbCardCache()
    cache.set_card(
        "card_en_forest",
        {
            "id": "abc",
            "lang": "en",
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "cmc": 0.0,
            "prices": {"eur": "0.05"},
        },
    )

    card = fetch_card_data("Forest", cache)
    assert card["name"] == "Forest"
    assert card["price_eur"] == 0.05
    assert card["image_paths"] == []  # no image_uris in the cached payload
    assert card["faces"][0]["rules_text"] == "({T}: Add {G}.)"


@pytest.mark.django_db
def test_fetch_card_data_returns_none_for_cached_not_found():
    cache = DbCardCache()
    cache.set_card("card_en_nope", {"error": "not_found"})
    assert fetch_card_data("Nope", cache) is None


@pytest.mark.django_db
def test_fetch_card_data_shares_one_cache_entry_across_both_dfc_spellings():
    """"A // B" and its front face alone are the same card, so one entry serves both."""
    from decks.models import ScryfallCard

    cache = DbCardCache()
    cache.set_card(
        "card_en_sink_into_stupor",
        {
            "id": "mdfc",
            "lang": "en",
            "name": "Sink into Stupor // Soporific Springs",
            "type_line": "Instant // Land",
            "cmc": 3.0,
        },
    )

    # No network: a miss would try to reach Scryfall and fail the test run.
    card = fetch_card_data("Sink into Stupor // Soporific Springs", cache)
    assert card is not None
    assert card["name"] == "Sink into Stupor // Soporific Springs"
    assert ScryfallCard.objects.count() == 1


@pytest.mark.django_db
def test_storing_an_image_twice_is_a_no_op():
    # Two concurrent imports can both see a miss and both download the image.
    cache = DbCardCache()
    cache.set_image("img_same_en.jpg", b"\x01")
    cache.set_image("img_same_en.jpg", b"\x02")
    assert cache.get_image("img_same_en.jpg") == b"\x01"


@pytest.mark.django_db
def test_a_card_inserted_concurrently_is_overwritten(monkeypatch):
    from django.db import IntegrityError

    from decks.models import ScryfallCard

    cache = DbCardCache()
    cache.set_card("card_en_bolt", {"name": "old"})

    def racing_update_or_create(**kwargs):
        raise IntegrityError("duplicate key")  # another import inserted it first

    monkeypatch.setattr(ScryfallCard.objects, "update_or_create", racing_update_or_create)
    cache.set_card("card_en_bolt", {"name": "new"})
    assert ScryfallCard.objects.get(pk="card_en_bolt").data == {"name": "new"}


@pytest.mark.django_db
def test_fetch_card_raw_returns_the_cached_json_untouched(monkeypatch):
    from decks import scryfall

    monkeypatch.setattr(scryfall, "_scryfall_get", lambda url: pytest.fail("no network"))
    raw = {"id": "abc", "oracle_id": "o-1", "name": "Forest", "keywords": [], "colors": []}
    cache = DbCardCache()
    cache.set_card("card_en_forest", raw)
    assert scryfall.fetch_card_raw("Forest", cache) == raw


@pytest.mark.django_db
def test_fetch_card_raw_is_none_for_a_cached_not_found(monkeypatch):
    from decks import scryfall

    monkeypatch.setattr(scryfall, "_scryfall_get", lambda url: pytest.fail("no network"))
    cache = DbCardCache()
    cache.set_card("card_en_nope", {"error": "not_found"})
    assert scryfall.fetch_card_raw("Nope", cache) is None


@pytest.mark.django_db
def test_fetch_card_raw_does_not_cache_an_unreachable_scryfall(monkeypatch):
    from decks import scryfall

    monkeypatch.setattr(scryfall, "_scryfall_get", lambda url: None)
    cache = DbCardCache()
    assert scryfall.fetch_card_raw("Lightning Bolt", cache) is None
    assert cache.get_card("card_en_lightning_bolt") is None
