"""The home page: what is running, what needs attention, how the decks are doing."""

import pytest

from playtest.models import Match

from .factories import make_deck, make_match, make_playtest


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def client(client, user):
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_home_shows_running_decks_attention_and_recent(client, user):
    deck = make_deck(user, name="Boros", fmt="duel")
    playtest = make_playtest(user, deck=deck, deck_name="Boros")
    make_match(user, playtest=playtest, seats=("Boros", "Rival"), status=Match.Status.RUNNING)
    page = client.get("/").content.decode()
    assert "Running now" in page and "Rival" in page and 'hx-get="/home/running"' in page
    assert "Your decks" in page and "Boros" in page
    assert "Boros is not checked" in page
    assert "Recent playtests" in page and f"/playtests/{playtest.id}" in page
    assert 'aria-current="page" href="/"' in page


@pytest.mark.django_db
def test_home_without_activity_hides_running_and_attention(client, user):
    make_deck(user, coverage={"unresolved": [], "unsupported_cards": []})
    page = client.get("/").content.decode()
    assert "Running now" not in page and "Needs attention" not in page


@pytest.mark.django_db
def test_the_running_panel_stops_polling_when_nothing_runs(client, user):
    assert client.get("/home/running").content.decode().strip() == ""


@pytest.mark.django_db
def test_a_deck_record_counts_only_its_own_playtests(client, user):
    deck = make_deck(user, name="Boros", fmt="duel", coverage={"unresolved": [], "unsupported_cards": []})
    match = make_match(user, playtest=make_playtest(user, deck=deck, deck_name="Boros"), status=Match.Status.FINISHED)
    match.winner = match.seats.get(position=0)
    match.save()
    make_match(user, status=Match.Status.FINISHED)  # another deck's playtest: a draw
    page = client.get("/").content.decode()
    assert "1–0–0" in page and "100%" in page
