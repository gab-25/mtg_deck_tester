"""Tests for metagame decks: the model and the sync (no network)."""

import pytest

from decks.models import MetagameDeck

from .factories import constructed_cards, make_playtest


def _meta(**fields):
    defaults = {
        "format": "modern",
        "source": MetagameDeck.Source.MTGTOP8,
        "archetype": "UR Aggro",
        "source_url": "https://www.mtgtop8.com/event?e=1&d=2&f=MO",
        "meta_share": 8.4,
        "rank": 1,
        "raw_decklist": "",
        "cards": constructed_cards(),
        "color_identity": ["R"],
        "coverage": {"unresolved": ["Nope Card", "Other Card"], "unsupported_cards": []},
    }
    defaults.update(fields)
    return MetagameDeck.objects.create(**defaults)


@pytest.mark.django_db
def test_a_metagame_deck_counts_its_main_deck_and_flags_cards_forge_does_not_know():
    deck = _meta()
    assert deck.total_cards == 60 and not deck.has_commander
    assert deck.forge_checked and deck.unknown_cards == ["Nope Card", "Other Card"]
    assert deck.label == "UR Aggro — 8.4% — 2 cards unknown to Forge"
    ready = _meta(coverage={"unresolved": [], "unsupported_cards": []})
    assert ready.forge_checked and not ready.unknown_cards and ready.label == "UR Aggro — 8.4%"
    unchecked = _meta(meta_share=None, coverage={})
    assert not unchecked.forge_checked and unchecked.label == "UR Aggro"


# -- the sync --------------------------------------------------------------------

from decks.cache import DbCardCache  # noqa: E402
from decks.metagame import MetagameEntry, SourceError  # noqa: E402
from decks.metagame.sync import sync_format  # noqa: E402

from .factories import legal_constructed_decklist  # noqa: E402


def _legal_fetch(fmt, limit, **kwargs):
    return [
        MetagameEntry("Goblins", "https://example.test/1", legal_constructed_decklist(), 12.5, 1),
        MetagameEntry("Broken", "https://example.test/2", "4 Missing Card\n56 Mountain\n", 3.0, 2),
    ][:limit]


@pytest.fixture
def fake_scryfall(monkeypatch):
    from decks import importer

    from .factories import scryfall_card

    def fetch(name, cache):
        if name == "Missing Card":
            return None
        if name == "Mountain":
            card = scryfall_card(name, "Basic Land — Mountain")
        else:
            card = scryfall_card(name, "Instant", 1.0)
        card["legalities"]["modern"] = "legal"
        return card

    monkeypatch.setattr(importer, "fetch_card_data", fetch)


@pytest.mark.django_db
def test_a_sync_imports_ready_and_failed_decks_with_their_coverage(fake_scryfall):
    report = sync_format("modern", 10, DbCardCache(), fetch=_legal_fetch, check=lambda cards: {"unresolved": ["Nope"], "unsupported_cards": []})
    assert (report.imported, report.failed, report.unknown_to_forge) == (1, 1, 1)
    ready = MetagameDeck.objects.get(archetype="Goblins")
    assert ready.status == MetagameDeck.Status.READY and ready.total_cards == 60 and ready.coverage == {"unresolved": ["Nope"], "unsupported_cards": []}
    assert ready.source == MetagameDeck.Source.MTGTOP8 and ready.meta_share == 12.5
    broken = MetagameDeck.objects.get(archetype="Broken")
    assert broken.status == MetagameDeck.Status.FAILED and "Missing Card" in broken.error


@pytest.mark.django_db
def test_a_new_sync_replaces_the_snapshot_and_seats_keep_their_name(fake_scryfall, django_user_model):
    from playtest.models import Match, Seat

    sync_format("modern", 1, DbCardCache(), fetch=_legal_fetch, check=lambda cards: {"unresolved": [], "unsupported_cards": []})
    old = MetagameDeck.objects.get()
    user = django_user_model.objects.create_user(username="u")
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt="modern"), format="modern", seed=1)
    seat = Seat.objects.create(match=match, position=0, metagame_deck=old, deck_name=old.archetype)
    sync_format("modern", 1, DbCardCache(), fetch=_legal_fetch, check=lambda cards: {"unresolved": [], "unsupported_cards": []})
    assert MetagameDeck.objects.count() == 1 and MetagameDeck.objects.get().pk != old.pk
    seat.refresh_from_db()
    assert seat.metagame_deck is None and seat.deck_name == "Goblins"


@pytest.mark.django_db
def test_a_failing_source_keeps_the_previous_snapshot(fake_scryfall):
    sync_format("modern", 1, DbCardCache(), fetch=_legal_fetch, check=lambda cards: {"unresolved": [], "unsupported_cards": []})

    def broken(fmt, limit, **kwargs):
        raise SourceError("MTGTop8 returned HTTP 503.")

    with pytest.raises(SourceError):
        sync_format("modern", 1, DbCardCache(), fetch=broken, check=lambda cards: {})
    assert MetagameDeck.objects.filter(format="modern").count() == 1


@pytest.mark.django_db
def test_the_command_reports_each_format_and_fails_at_the_end(monkeypatch, fake_scryfall):
    import io

    from django.core.management import CommandError, call_command

    from decks.metagame import sync

    def fake_sync(fmt, limit, cache, **kwargs):
        if fmt == "pauper":
            raise SourceError("down")
        return sync.SyncReport(fmt, 2, 1, 1 if fmt == "modern" else None)

    monkeypatch.setattr(sync, "sync_format", fake_sync)
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError, match="pauper"):
        call_command("sync_metagame", stdout=out, stderr=err)
    assert "modern: 2 imported, 1 failed, 1 with cards Forge does not know" in out.getvalue()
    assert "legacy: 2 imported, 1 failed, not checked (Forge bridge unreachable)" in out.getvalue()
    assert "pauper: down" in err.getvalue()


@pytest.mark.django_db
def test_a_sync_where_every_deck_fails_keeps_the_snapshot(fake_scryfall):
    sync_format("modern", 1, DbCardCache(), fetch=_legal_fetch, check=lambda cards: {"unresolved": [], "unsupported_cards": []})

    def garbage(fmt, limit, **kwargs):
        return [MetagameEntry("Oops", "https://example.test/x", "<html><body>Error</body></html>", None, 1)]

    with pytest.raises(SourceError, match="no deck could be imported"):
        sync_format("modern", 1, DbCardCache(), fetch=garbage, check=lambda cards: {})
    assert MetagameDeck.objects.get(format="modern").archetype == "Goblins"


@pytest.mark.django_db
def test_only_web_links_are_kept_as_source_urls(fake_scryfall):
    def sneaky(fmt, limit, **kwargs):
        [entry] = _legal_fetch(fmt, 1)
        return [MetagameEntry(entry.archetype, "javascript:alert(1)", entry.decklist, None, 1)]

    sync_format("modern", 1, DbCardCache(), fetch=sneaky, check=lambda cards: {})
    assert MetagameDeck.objects.get().source_url == ""


@pytest.mark.django_db
def test_the_default_check_asks_forge_and_tolerates_a_bridge_that_is_down(fake_forge, monkeypatch):
    from decks.metagame.sync import forge_check
    from playtest import forge

    fake_forge.unknown = {"Spell 2"}
    assert forge_check("modern")(constructed_cards()) == {"unresolved": ["Spell 2"], "unsupported_cards": []}

    def down(*args, **kwargs):
        raise forge.ForgeError("unreachable")

    monkeypatch.setattr(fake_forge, "check_deck", down)
    assert forge_check("modern")(constructed_cards()) == {}
