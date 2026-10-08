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
def test_heuristic_agents_play_a_duel_from_the_web(client, user):
    assert b'value="heuristic"' in client.get("/matches/new").content
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/matches/create", _form("duel", decks, agent="heuristic", max_rounds="40"))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED
    assert {seat.agent for seat in match.seats.all()} == {"heuristic"}
    assert b"Heuristic" in client.get(f"/matches/{match.id}").content


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
