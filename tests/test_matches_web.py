"""Web tests for setting up and following a match."""

import pytest

from playtest.models import Match

from .factories import make_deck


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def client(client, user, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client.force_login(user)
    return client


def _form(fmt, decks, agent="random", **extra):
    data = {"format": fmt, "max_rounds": "15", "seed": "7"}
    for i, deck in enumerate(decks):
        data[f"seat-{i}-deck"] = str(deck.id)
        data[f"seat-{i}-agent"] = agent
    data.update(extra)
    return data


@pytest.mark.django_db
def test_new_match_form_lists_my_ready_decks(client, user):
    make_deck(user, name="Ready Bears")
    make_deck(user, name="Still Importing", status="processing")
    content = client.get("/matches/new").content
    assert b"Ready Bears" in content
    assert b"Still Importing" not in content


@pytest.mark.django_db
def test_new_match_form_prefills_the_deck_from_its_page(client, user):
    deck = make_deck(user, fmt="duel")
    content = client.get(f"/matches/new?deck={deck.id}&format=duel").content.decode()
    assert f'value="{deck.id}" selected' in content


@pytest.mark.django_db
def test_a_duel_between_random_agents_runs_to_the_end(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks))
    match = Match.objects.get(owner=user)
    assert response.status_code == 302
    assert response["Location"] == f"/matches/{match.id}"
    assert match.status == Match.Status.FINISHED
    assert match.seed == 7
    assert match.max_rounds == 15

    page = client.get(f"/matches/{match.id}").content
    assert b"Game log" in page
    assert b"goes first" in page
    # The finished region no longer polls.
    assert b"hx-trigger" not in client.get(f"/matches/{match.id}/live").content


@pytest.mark.django_db
def test_a_four_player_commander_pod(client, user):
    decks = [make_deck(user, name=f"Deck {i}") for i in range(4)]
    client.post("/matches/create", _form("commander", decks))
    match = Match.objects.get(owner=user)
    assert match.seats.count() == 4
    assert match.status == Match.Status.FINISHED


@pytest.mark.django_db
def test_the_same_deck_can_sit_twice(client, user):
    deck = make_deck(user, fmt="duel")
    client.post("/matches/create", _form("duel", [deck, deck]))
    match = Match.objects.get(owner=user)
    assert match.seats.count() == 2
    # The log tells the two apart.
    setup = match.events.get(kind="setup")
    assert setup.payload["decks"] == ["Bears (seat 1)", "Bears (seat 2)"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fmt, seats, message",
    [
        ("duel", 3, b"exactly 2 players; 3 chosen"),
        ("commander", 1, b"2 to 4 players; 1 chosen"),
    ],
)
def test_the_seat_count_must_fit_the_format(client, user, fmt, seats, message):
    decks = [make_deck(user, name=f"Deck {i}", fmt=fmt) for i in range(seats)]
    response = client.post("/matches/create", _form(fmt, decks))
    assert response.status_code == 422
    assert message in response.content
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_decks_must_match_the_format(client, user):
    decks = [make_deck(user, name="Pod deck"), make_deck(user, name="Duel deck", fmt="duel")]
    response = client.post("/matches/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"Pod deck is not a Duel Commander deck" in response.content


@pytest.mark.django_db
def test_someone_elses_deck_cannot_be_seated(client, user, django_user_model):
    theirs = make_deck(django_user_model.objects.create_user(username="other"), fmt="duel")
    mine = make_deck(user, fmt="duel")
    response = client.post("/matches/create", _form("duel", [mine, theirs]))
    assert response.status_code == 422
    assert b"pick one of your imported decks" in response.content


@pytest.mark.django_db
def test_llm_seats_need_an_api_key(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks, agent="llm"))
    assert response.status_code == 422
    assert b"OPENROUTER_API_KEY" in response.content


@pytest.mark.django_db
def test_an_llm_match_records_the_reasoning(client, user, monkeypatch):
    from playtest.agents import llm_agent

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_agent.openrouter, "chat", lambda messages, model: '{"choice": 1, "reason": "first is best"}'
    )
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/matches/create", _form("duel", decks, agent="llm", max_rounds="3"))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED
    assert match.events.filter(reasoning="first is best").exists()
    assert b"first is best" in client.get(f"/matches/{match.id}").content


@pytest.mark.django_db
def test_bad_numbers_are_reported(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks, seed="abc", max_rounds="0"))
    assert response.status_code == 422
    assert b"seed must be a whole number" in response.content
    assert b"round limit must be between 1 and 100" in response.content


@pytest.mark.django_db
def test_someone_elses_match_is_a_404(client, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    match = Match.objects.create(owner=other, seed=1)
    assert client.get(f"/matches/{match.id}").status_code == 404
    assert client.get(f"/matches/{match.id}/live").status_code == 404
    assert client.post(f"/matches/{match.id}/delete").status_code == 404


@pytest.mark.django_db
def test_a_running_match_polls_its_live_region(client, user):
    match = Match.objects.create(owner=user, seed=1, status=Match.Status.RUNNING)
    assert b"hx-trigger" in client.get(f"/matches/{match.id}/live").content


@pytest.mark.django_db
def test_deleting_a_match(client, user):
    match = Match.objects.create(owner=user, seed=1)
    assert client.post(f"/matches/{match.id}/delete").status_code == 302
    assert not Match.objects.exists()


# -- benchmarks, coverage, confidence and the summary ---------------------------------


def test_every_benchmark_deck_compiles_to_full_coverage():
    from decks.formats import COMMANDER_FAMILY
    from playtest import benchmarks
    from playtest.engine.coverage import coverage_report

    for key in benchmarks.BENCHMARKS:
        entries = benchmarks.entries(key)
        report = coverage_report([e.coverage() for e in entries])
        commanders = sum(1 for e in entries if e.is_commander)
        if benchmarks.family(key) == COMMANDER_FAMILY:
            assert report["total"] == 100 and commanders == 1, key
        else:
            assert report["total"] == 60 and commanders == 0, key
        assert report["percent"] == 100 and not report["unsupported_cards"], key


def test_benchmarks_are_offered_by_family():
    from playtest import benchmarks

    assert {k for k, _ in benchmarks.choices("constructed")} == {"aggro60", "midrange60", "control60"}
    assert {k for k, _ in benchmarks.choices("commander")} == {"aggro", "midrange", "control"}


@pytest.mark.django_db
@pytest.mark.parametrize("fmt", ["duel", "commander"])
def test_a_benchmark_deck_can_sit_at_any_table_with_no_scryfall_call(client, user, fmt):
    deck = make_deck(user, fmt=fmt)
    data = {
        "format": fmt,
        "max_rounds": "6",
        "seed": "3",
        "seat-0-deck": str(deck.id),
        "seat-0-agent": "heuristic",
        "seat-1-deck": "benchmark:control",
        "seat-1-agent": "heuristic",
    }
    assert client.post("/matches/create", data).status_code == 302
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    seat = match.seats.get(position=1)
    assert (seat.benchmark, seat.deck, seat.deck_name) == ("control", None, "Benchmark: Control")
    assert seat.coverage["percent"] == 100


@pytest.mark.django_db
def test_the_form_offers_the_benchmarks_and_the_new_agents(client):
    content = client.get("/matches/new").content.decode()
    for value in ("benchmark:aggro", "benchmark:midrange", "benchmark:control"):
        assert f'value="{value}"' in content
    for agent in ("heuristic", "jev", "llm", "random"):
        assert f'value="{agent}"' in content


@pytest.mark.django_db
def test_jev_seats_need_an_api_key(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks, agent="jev"))
    assert response.status_code == 422
    assert b"OPENROUTER_API_KEY" in response.content


def _finished(user, **seat_fields):
    from playtest.models import MatchEvent, Seat

    match = Match.objects.create(owner=user, seed=1, status=Match.Status.FINISHED, round=3, end_reason="turn_limit")
    for position in range(2):
        fields = {"deck_name": f"Deck {position}", "agent": "jev", **(seat_fields if position == 0 else {})}
        Seat.objects.create(match=match, position=position, **fields)
    return match, MatchEvent


@pytest.mark.django_db
def test_seat_cards_show_coverage_warnings_and_unsupported_cards(client, user):
    coverage = {
        "total": 100,
        "supported": 90,
        "partial": 0,
        "unsupported": 10,
        "percent": 90,
        "commander_unsupported": True,
        "dead_spells": 3,
        "reasons": [],
        "unsupported_cards": [
            {"name": "Odd Commander", "quantity": 1, "commander": True, "spell": False, "kind": "none", "reason": "clause 1: no operation in the DSL fits"},
            {"name": "Tutor", "quantity": 1, "commander": False, "spell": True, "kind": "blocklisted", "reason": "library search"},
        ],
    }
    match, _ = _finished(user, coverage=coverage)
    page = client.get(f"/matches/{match.id}").content.decode()
    assert "90%" in page
    assert "A partial simulation" in page
    assert "commander’s abilities could not be compiled" in page
    assert "3 dead spells" in page
    assert "Tutor</span> — library search" in page


@pytest.mark.django_db
def test_the_log_shows_jev_confidence_and_marks_low_confidence_fallbacks(client, user):
    match, MatchEvent = _finished(user)
    probabilities = [{"label": "Attack Deck 1", "p": 0.82}, {"label": "Pass priority", "p": 0.11}]
    MatchEvent.objects.create(
        match=match, seq=1, round=1, turn=1, seat_position=0, kind="decision",
        text="Deck 0 chooses: Attack Deck 1.", payload={"confidence": 0.82, "probabilities": probabilities, "model": "typesafe/jev-1.13"},
    )
    MatchEvent.objects.create(
        match=match, seq=2, round=1, turn=1, seat_position=0, kind="agent_fallback",
        text="Deck 0's agent fell back to heuristic.", payload={"confidence": 0.31, "fallback_kind": "low_confidence", "probabilities": probabilities},
    )
    page = client.get(f"/matches/{match.id}").content.decode()
    assert "0.82" in page and "runner-up: Pass priority" in page
    assert "Low confidence 0.31" in page


@pytest.mark.django_db
def test_a_finished_match_shows_its_summary(client, user):
    match, _ = _finished(user)
    assert "Match summary" not in client.get(f"/matches/{match.id}").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/matches/create", _form("duel", decks, agent="heuristic"))
    played = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/matches/{played.id}").content.decode()
    assert "Match summary" in page and "Damage dealt" in page and "paid automatically" in page


@pytest.mark.django_db
def test_the_deck_page_shows_coverage_only_from_the_cache(client, user):
    deck = make_deck(user)
    assert "Playtest coverage" in client.get(f"/decks/{deck.id}").content.decode()
    from decks.models import ScryfallCard

    ScryfallCard.objects.filter(key="card_en_forest").delete()
    # A card missing from the cache: no coverage, and no network call to fill it in.
    assert "Playtest coverage" not in client.get(f"/decks/{deck.id}").content.decode()
