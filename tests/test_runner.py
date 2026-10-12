"""Tests for running a stored match through the engine."""

import pytest

from playtest import judge
from playtest.models import Match, MatchEvent, Seat
from playtest.runner import deck_spec, run_match

from .cards import FakeJev
from .factories import make_deck, scryfall_card, stored_cards


@pytest.fixture(autouse=True)
def no_scryfall(monkeypatch):
    """No raw card in the cache: the compiler falls back to the stored cards."""
    monkeypatch.setattr(judge, "fetch_card_raw", lambda name, cache: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _entries(cards):
    return judge.compile_deck(cards, cache=_NoRules(), ask=FakeJev())


class _NoRules:
    def get_rule(self, *args):
        return None

    def set_rule(self, *args, **kwargs):
        pass


def test_deck_spec_expands_quantities_and_separates_the_commander():
    spec = deck_spec("Bears", _entries(stored_cards()))
    assert spec.commander.name == "Bear Lord"
    assert spec.commander.power == 5
    assert len(spec.library) == 99


def test_deck_spec_requires_a_commander():
    cards = [item for item in stored_cards() if not item["is_commander"]]
    with pytest.raises(ValueError, match="no commander"):
        deck_spec("Bears", _entries(cards))


def test_unsupported_cards_enter_as_blanks():
    cards = stored_cards()
    weird = scryfall_card("Weird Spell", "Instant", 1.0)
    weird["faces"][0]["rules_text"] = "Search your library for a card."
    cards.append({"quantity": 1, "is_commander": False, "data": weird})
    spec = deck_spec("Bears", _entries(cards))
    blank = next(c for c in spec.library if c.name == "Weird Spell")
    assert blank.program is None and blank.is_spell_only


def _match(user, fmt="duel", seats=2, **fields):
    match = Match.objects.create(owner=user, format=fmt, seed=5, **fields)
    for position in range(seats):
        deck = make_deck(user, name=f"Deck {position}", fmt=fmt)
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=Seat.Agent.HEURISTIC)
    return match


@pytest.mark.django_db
def test_run_match_stores_the_log_the_coverage_and_the_summary(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user, max_rounds=40)
    run_match(match.id)

    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED, match.error
    assert match.finished_at is not None
    events = list(MatchEvent.objects.filter(match=match))
    assert events[0].kind == "setup"
    assert events[-1].kind == "game_over"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.step for e in events)
    seats = list(match.seats.all())
    assert all(seat.life is not None for seat in seats)
    assert all(seat.coverage["percent"] == 100 for seat in seats)
    assert match.summary["rounds"] == match.round
    assert len(match.summary["seats"]) == 2
    if match.end_reason == Match.EndReason.LAST_STANDING:
        assert match.winner in seats


@pytest.mark.django_db
def test_run_match_is_reproducible(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    logs = []
    for _ in range(2):
        match = _match(user, max_rounds=10)
        run_match(match.id)
        logs.append(list(match.events.values_list("text", flat=True)))
    assert logs[0] == logs[1]


@pytest.mark.django_db
def test_a_never_compiled_deck_plays_with_no_network(django_user_model):
    """No key, no cache: cards needing the judge become UNSUPPORTED, the match runs."""
    user = django_user_model.objects.create_user(username="u")
    match = _match(user, max_rounds=5)
    deck = match.seats.first().deck
    bolt = scryfall_card("Zap", "Instant", 1.0)
    bolt["faces"][0]["rules_text"] = "Zap deals 3 damage to any target."
    deck.cards = deck.cards[:-1] + [{"quantity": 1, "is_commander": False, "data": bolt}]
    deck.save()
    run_match(match.id)
    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED
    coverage = match.seats.get(position=0).coverage
    assert coverage["dead_spells"] == 1
    assert coverage["unsupported_cards"][0]["kind"] == "not_asked"


@pytest.mark.django_db
def test_a_match_whose_deck_was_deleted_fails_cleanly(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user)
    match.seats.first().deck.delete()
    run_match(match.id)
    match.refresh_from_db()
    assert match.status == Match.Status.FAILED
    assert "deleted" in match.error


@pytest.mark.django_db
def test_a_constructed_match_plays_the_main_deck_only(django_user_model):
    from .factories import make_constructed_deck

    user = django_user_model.objects.create_user(username="u")
    match = Match.objects.create(owner=user, format="modern", seed=5, max_rounds=30)
    for position in range(2):
        deck = make_constructed_deck(user, name=f"Goblins {position}")
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=Seat.Agent.HEURISTIC)
    run_match(match.id)
    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED, match.error
    assert match.events.first().payload["starting_life"] == 20
    assert not [e for e in match.events.filter(kind="cast") if e.payload.get("commander")]
    for seat in match.seats.all():
        assert seat.coverage["total"] == 60 and not seat.coverage["commander_unsupported"]
    assert match.summary["has_commander"] is False


def test_deck_spec_without_a_commander():
    from .factories import constructed_cards

    entries = _entries([c for c in constructed_cards() if c["board"] == "main"])
    spec = deck_spec("Goblins", entries, has_commander=False)
    assert spec.commander is None and len(spec.library) == 60
