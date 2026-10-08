"""Tests for running a stored match through the engine."""

import pytest

from playtest.agents.heuristic import HeuristicAgent
from playtest.engine.cards import Kind
from playtest.engine.manacost import ManaCost
from playtest.models import Match, MatchEvent, Seat
from playtest.runner import build_agent, card_spec, deck_spec, run_match

from .factories import make_deck, scryfall_card, stored_cards


@pytest.mark.parametrize(
    "type_line, kind",
    [
        ("Basic Land — Forest", Kind.LAND),
        ("Legendary Creature — Elf", Kind.CREATURE),
        ("Artifact", Kind.PERMANENT),
        ("Legendary Planeswalker — Jace", Kind.PERMANENT),
        ("Instant", Kind.SPELL),
        ("Sorcery", Kind.SPELL),
    ],
)
def test_card_spec_maps_the_card_type(type_line, kind):
    assert card_spec(scryfall_card("X", type_line)).kind == kind


@pytest.mark.parametrize("power, expected", [("3", 3), ("1+*", 1), ("*", 0), (None, 0)])
def test_card_spec_reads_printed_power(power, expected):
    assert card_spec(scryfall_card("X", "Creature", power=power)).power == expected


def test_card_spec_reads_the_facts_off_raw_scryfall_json():
    raw = {
        "name": "Llanowar Elves",
        "oracle_id": "x",
        "type_line": "Creature — Elf Druid",
        "mana_cost": "{G}",
        "cmc": 1.0,
        "power": "1",
        "toughness": "1",
        "colors": ["G"],
        "produced_mana": ["G"],
        "oracle_text": "{T}: Add {G}.",
    }
    spec = card_spec(raw)
    assert spec.kind == Kind.CREATURE
    assert spec.cost == ManaCost(0, (("G", 1),))
    assert spec.colors == ("G",)
    assert (spec.produced_mana, spec.mana_output) == (("G",), 1)


def test_card_spec_reads_keywords_and_enters_tapped():
    raw = {
        "name": "Serra Angel",
        "type_line": "Creature — Angel",
        "mana_cost": "{3}{W}{W}",
        "cmc": 5.0,
        "power": "4",
        "toughness": "4",
        "oracle_text": "Flying, vigilance",
    }
    assert card_spec(raw).keywords == ("flying", "vigilance")
    tapland = {"name": "Guildgate", "type_line": "Land — Gate", "produced_mana": ["W", "U"],
               "oracle_text": "This land enters tapped.\n{T}: Add {W} or {U}."}
    assert card_spec(tapland).enters_tapped


def test_deck_spec_expands_quantities_and_separates_the_commander():
    spec = deck_spec("Bears", stored_cards())
    assert spec.commander.name == "Bear Lord"
    assert len(spec.library) == 99
    forest = next(c for c in spec.library if c.name == "Forest")
    assert forest.produced_mana == ("G",)


def test_deck_spec_prefers_the_raw_scryfall_json():
    raw = {"name": "Forest", "type_line": "Basic Land — Forest", "produced_mana": ["G"], "oracle_text": ""}
    fetched = []

    def fetch(name):
        fetched.append(name)
        return raw if name == "Forest" else None

    spec = deck_spec("Bears", stored_cards(), fetch=fetch)
    assert "Forest" in fetched and "Bear Lord" in fetched
    assert spec.library[0].facts.type_line == "Basic Land — Forest"


def test_a_heuristic_seat_gets_the_heuristic_agent():
    assert isinstance(build_agent(Seat(position=0, agent=Seat.Agent.HEURISTIC), 1), HeuristicAgent)


def test_deck_spec_requires_a_commander():
    cards = [item for item in stored_cards() if not item["is_commander"]]
    with pytest.raises(ValueError, match="no commander"):
        deck_spec("Bears", cards)


def _match(user, fmt="duel", seats=2, agent=Seat.Agent.RANDOM, **fields):
    match = Match.objects.create(owner=user, format=fmt, seed=5, **fields)
    for position in range(seats):
        deck = make_deck(user, name=f"Deck {position}", fmt=fmt)
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=agent)
    return match


@pytest.mark.django_db
def test_run_match_stores_the_log_and_the_result(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user, max_rounds=40)
    run_match(match.id)

    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED
    assert match.finished_at is not None
    events = list(MatchEvent.objects.filter(match=match))
    assert events[0].kind == "setup"
    assert {"main1", "declare_attackers"} <= {e.step for e in events}
    assert events[-1].kind == "game_over"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    seats = list(match.seats.all())
    assert all(seat.life is not None for seat in seats)
    if match.end_reason == Match.EndReason.LAST_STANDING:
        assert match.winner in seats


@pytest.mark.django_db
def test_a_heuristic_match_plays_to_a_winner(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user, fmt="commander", seats=4, agent=Seat.Agent.HEURISTIC, max_rounds=60)
    run_match(match.id)
    match.refresh_from_db()
    assert match.status == Match.Status.FINISHED
    assert match.end_reason == Match.EndReason.LAST_STANDING
    assert match.winner is not None
    assert not match.events.filter(kind__in=["warning", "limit", "decision_limit"]).exists()


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
def test_a_match_whose_deck_was_deleted_fails_cleanly(django_user_model):
    user = django_user_model.objects.create_user(username="u")
    match = _match(user)
    match.seats.first().deck.delete()
    run_match(match.id)
    match.refresh_from_db()
    assert match.status == Match.Status.FAILED
    assert "deleted" in match.error
