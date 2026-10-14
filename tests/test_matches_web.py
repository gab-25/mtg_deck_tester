"""Web tests for setting up and following a match."""

import pytest

from playtest.models import Match, Playtest

from .factories import make_deck, make_match, make_playtest


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def jev(monkeypatch):
    """Jev plays every seat: a key and a stand-in answer, no network."""
    from playtest.agents import typesafe

    from .test_jev_agent import StubJev

    stub = StubJev(confidence=0.9)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", stub)
    return stub


@pytest.fixture
def client(client, user, jev):
    client.force_login(user)
    return client


def _form(fmt, decks, **extra):
    """The new-playtest form: the first deck is tested against the others."""
    data = {"format": fmt, "seed": "7"}
    if decks:
        data["deck"] = str(decks[0].id)
        data["opponents"] = [str(deck.id) for deck in decks[1:]]
    data.update(extra)
    return data


@pytest.mark.django_db
def test_new_match_form_lists_my_ready_decks(client, user):
    make_deck(user, name="Ready Bears", fmt="duel")
    make_deck(user, name="Still Importing", fmt="duel", status="processing")
    content = client.get("/playtests/new").content
    assert b"Ready Bears" in content
    assert b"Still Importing" not in content


@pytest.mark.django_db
def test_new_match_form_prefills_the_deck_from_its_page(client, user):
    deck = make_deck(user, fmt="duel")
    content = client.get(f"/playtests/new?deck={deck.id}&format=duel").content.decode()
    assert f'value="{deck.id}" data-format="duel" selected' in content


@pytest.mark.django_db
def test_a_deck_is_tested_against_each_opponent(client, user, fake_forge):
    tested, *opponents = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(3)]
    response = client.post("/playtests/create", _form("duel", [tested, *opponents]))
    playtest = Playtest.objects.get(owner=user)
    assert response.status_code == 302
    assert response["Location"] == f"/playtests/{playtest.id}"
    assert playtest.deck == tested and playtest.seed == 7
    matches = list(playtest.matches.order_by("seed"))
    assert [m.seed for m in matches] == [7, 8]
    for match, opponent in zip(matches, opponents):
        assert match.status == Match.Status.FINISHED, match.error
        assert [(s.position, s.deck_id, s.agent) for s in match.seats.all()] == [(0, tested.id, "jev"), (1, opponent.id, "jev")]
    assert [g["seed"] for g in fake_forge.created] == [7, 8]

    page = client.get(f"/playtests/{playtest.id}").content.decode()
    assert "vs" in page and "Deck 1" in page and "Deck 2" in page
    assert f"/playtests/{playtest.id}/matches/{matches[0].id}" in page
    # Every match ended: the session no longer polls.
    assert b"hx-trigger" not in client.get(f"/playtests/{playtest.id}/live").content

    log = client.get(f"/playtests/{playtest.id}/matches/{matches[0].id}").content.decode()
    assert "Game log" in log and "Turn 1 (Deck 0)" in log and "Back to the playtest" in log
    # Every step of every turn stays out of the page.
    assert "{T}: Add {R}" not in log


@pytest.mark.django_db
def test_multiplayer_commander_cannot_be_played_yet(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}") for i in range(2)]
    response = client.post("/playtests/create", _form("commander", decks))
    assert response.status_code == 422
    assert b"Commander cannot be played yet" in response.content
    assert not Match.objects.exists() and not fake_forge.created
    assert b'<option value="commander"' not in client.get("/playtests/new").content


@pytest.mark.django_db
def test_the_same_deck_can_sit_twice(client, user, fake_forge):
    deck = make_deck(user, fmt="duel")
    client.post("/playtests/create", _form("duel", [deck, deck]))
    match = Match.objects.get(owner=user)
    assert match.seats.count() == 2
    # Forge tells the two apart.
    assert [s["name"] for s in fake_forge.created[0]["seats"]] == ["Bears (seat 1)", "Bears (seat 2)"]


@pytest.mark.django_db
def test_a_playtest_needs_a_deck_and_at_least_one_opponent(client, user):
    deck = make_deck(user, name="Deck 0", fmt="duel")
    response = client.post("/playtests/create", _form("duel", [deck]))
    assert response.status_code == 422 and b"Pick at least one opponent" in response.content
    response = client.post("/playtests/create", {"format": "duel", "opponents": [str(deck.id)]})
    assert response.status_code == 422 and b"Pick the deck to test" in response.content
    assert not Playtest.objects.exists() and not Match.objects.exists()


@pytest.mark.django_db
def test_a_playtest_has_at_most_ten_opponents(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(12)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422 and b"at most 10 opponents; 11 chosen" in response.content


@pytest.mark.django_db
def test_decks_must_match_the_format(client, user):
    decks = [make_deck(user, name="Pod deck"), make_deck(user, name="Duel deck", fmt="duel")]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"Pod deck is not a Duel Commander deck" in response.content


@pytest.mark.django_db
def test_someone_elses_deck_cannot_be_tested_or_played_against(client, user, django_user_model):
    theirs = make_deck(django_user_model.objects.create_user(username="other"), fmt="duel")
    mine = make_deck(user, fmt="duel")
    response = client.post("/playtests/create", _form("duel", [mine, theirs]))
    assert response.status_code == 422 and b"Pick opponents among your imported decks" in response.content
    response = client.post("/playtests/create", _form("duel", [theirs, mine]))
    assert response.status_code == 422 and b"Pick the deck to test" in response.content


@pytest.mark.django_db
def test_a_match_needs_an_api_key_since_jev_plays_every_seat(client, user, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"OPENROUTER_API_KEY" in response.content
    assert b"no playtest can start" in client.get("/playtests/new").content


@pytest.mark.django_db
def test_a_deck_forge_does_not_know_is_refused_naming_the_card(client, user, fake_forge):
    tested = make_deck(user, name="Deck 0", fmt="duel")
    good = make_deck(user, name="Deck 1", fmt="duel")
    odd = make_deck(user, name="Odd", fmt="duel")
    odd.cards[2]["data"]["name"] = "Nope Card"
    odd.save()
    fake_forge.unknown = {"Nope Card"}
    response = client.post("/playtests/create", _form("duel", [tested, good, odd]))
    assert response.status_code == 422
    assert "Odd: Forge does not know Nope Card." in response.content.decode()
    # The whole session is refused, and the game already started is ended.
    assert not Playtest.objects.exists() and not Match.objects.exists()
    assert [g.status for g in fake_forge.games.values()] == ["aborted"]


@pytest.mark.django_db
def test_an_unreachable_bridge_is_reported_and_no_match_is_left(client, user, monkeypatch):
    from playtest import forge

    def down(*args, **kwargs):
        raise forge.ForgeError("Forge bridge unreachable at http://localhost:8080: refused")

    monkeypatch.setattr(forge.ForgeClient, "create_game", down)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 503
    assert b"Forge bridge unreachable" in response.content
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_bad_numbers_are_reported(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks, seed="abc"))
    assert response.status_code == 422
    assert b"seed must be a whole number" in response.content


@pytest.mark.django_db
def test_someone_elses_match_is_a_404(client, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    match = make_match(other)
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").status_code == 404
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}/live").status_code == 404
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop").status_code == 404
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/delete").status_code == 404


@pytest.mark.django_db
def test_a_running_match_polls_its_live_region_and_can_be_stopped(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content
    assert b"hx-trigger" in page and b"Stop" in page
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop").status_code == 302
    assert fake_forge.games[game["id"]].status == "aborted"


@pytest.mark.django_db
def test_deleting_a_running_match_aborts_its_game(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    assert client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/delete").status_code == 302
    assert not Match.objects.exists()
    assert fake_forge.games[game["id"]].status == "aborted"


# -- coverage, confidence and the summary ---------------------------------------------


@pytest.mark.django_db
def test_the_form_offers_no_agent_choice(client):
    content = client.get("/playtests/new").content.decode()
    assert 'name="seat-0-agent"' not in content
    assert "JEV PLAYS EVERY SEAT" in content


@pytest.mark.django_db
def test_a_posted_agent_is_ignored_every_seat_is_jev(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks, **{"seat-0-agent": "forge_ai", "seat-1-agent": "random"}))
    match = Match.objects.get(owner=user)
    assert list(match.seats.values_list("agent", flat=True)) == ["jev", "jev"]
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["external", "external"]


def _finished(user, **seat_fields):
    from playtest.models import MatchEvent, Seat

    match = make_match(user, status=Match.Status.FINISHED, turn=3, end_reason="turn_limit")
    if seat_fields:
        Seat.objects.filter(match=match, position=0).update(**seat_fields)
    return match, MatchEvent


@pytest.mark.django_db
def test_seat_cards_show_forges_coverage(client, user):
    match, _ = _finished(user, coverage={"unsupported_cards": ["Odd Card"]})
    match.seats.filter(position=1).update(coverage={"unsupported_cards": []})
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "1 unsupported" in page and "Odd Card" in page
    assert "Forge plays every card of this deck" in page
    assert "Turn limit reached after 3 turns" in page


@pytest.mark.django_db
def test_the_log_shows_confidence_fallback_reasons_and_folds_forge_ai_decisions(client, user):
    match, MatchEvent = _finished(user)
    probabilities = [{"label": "Attack with Bear", "p": 0.82}, {"label": "Pass priority", "p": 0.11}]
    rows = [
        ("decision", 0, "attack: Attack with Bear", {"kind": "attack", "choice": 1, "confidence": 0.82, "probabilities": probabilities, "model": "typesafe/jev-1.13"}),
        ("fallback", 0, "block: decided by Forge AI (delegate)", {"kind": "block", "reason": "delegate", "fallback_kind": "low_confidence", "confidence": 0.11, "probabilities": probabilities}),
        ("forge_ai", 0, "declareBlockers: decided by Forge AI", {"method": "declareBlockers"}),
        ("fallback", 1, "priority: decided by Forge AI (timeout)", {"kind": "priority", "reason": "timeout"}),
        ("forge_ai", 1, "chooseSpellAbilityToPlay: decided by Forge AI", {"method": "chooseSpellAbilityToPlay"}),
        ("forge_ai", 0, "assignCombatDamage: decided by Forge AI", {"method": "assignCombatDamage"}),
        ("forge_ai", 0, "payManaCost: decided by Forge AI", {"method": "payManaCost"}),
        ("forge_ai", 0, "payManaCost: decided by Forge AI", {"method": "payManaCost"}),
        ("log", None, "Red's Untap step", {"type": "PHASE"}),
    ]
    for seq, (kind, seat, text, payload) in enumerate(rows):
        MatchEvent.objects.create(match=match, seq=seq, turn=1, seat_position=seat, kind=kind, text=text, payload=payload)
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "0.82" in page and "runner-up: Pass priority" in page
    assert "low_confidence" in page and "Jev leaned to: Attack with Bear" in page
    assert "timeout" in page
    assert "assignCombatDamage, payManaCost (3×)" in page
    # A fallback's own Forge-AI decision is not repeated.
    assert "chooseSpellAbilityToPlay" not in page and "declareBlockers" not in page
    assert "Untap step" not in page


@pytest.mark.django_db
def test_a_finished_match_shows_its_summary(client, user, fake_forge):
    match, _ = _finished(user)
    assert "Match summary" not in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks))
    played = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/playtests/{played.playtest_id}/matches/{played.id}").content.decode()
    assert "Match summary" in page and "Spells cast" in page and "Fallbacks" in page
    assert "Automatic for every seat: mana payment" in page and "assignCombatDamage" in page
    assert "% Jev" in page


@pytest.mark.django_db
def test_a_constructed_match_between_two_jev_agents(client, user, fake_forge, monkeypatch):
    from playtest.agents import typesafe

    from .factories import make_constructed_deck
    from .test_jev_agent import StubJev

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", StubJev())
    decks = [make_constructed_deck(user, name=f"Goblins {i}") for i in range(2)]
    client.post("/playtests/create", _form("modern", decks))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["external", "external"]
    assert set(match.events.filter(kind="decision").values_list("seat_position", flat=True)) == {0, 1}


@pytest.mark.django_db
def test_the_new_match_form_tags_decks_by_format(client, user):
    from .factories import make_constructed_deck

    make_constructed_deck(user, name="Goblins")
    page = client.get("/playtests/new").content.decode()
    assert 'data-format="modern"' in page
    assert "benchmark" not in page


@pytest.mark.django_db
def test_hidden_decks_and_opponents_are_cleared_not_just_hidden(client):
    page = client.get("/playtests/new").content.decode()
    # Decks and opponents of another format must not stay selected and be posted unseen.
    assert 'deck.value = ""' in page and "checked = false" in page


def _metagame(fmt="modern", **fields):
    from decks.models import MetagameDeck

    from .factories import constructed_cards

    cards = constructed_cards(fmt)
    defaults = {"source": "mtgtop8", "archetype": "Goblins", "meta_share": 8.4, "rank": 1, "cards": cards,
                "coverage": {"unresolved": ["Odd Goblin"], "unsupported_cards": []}}
    defaults.update(fields)
    return MetagameDeck.objects.create(format=fmt, **defaults)


@pytest.mark.django_db
def test_the_form_offers_metagame_decks_warning_of_cards_forge_does_not_know(client):
    deck = _metagame()
    page = client.get("/playtests/new").content.decode()
    assert f'name="opponents" value="metagame:{deck.id}"' in page
    assert "Goblins — 8.4% — 1 card unknown to Forge</span><span class=\"text-gold\">⚠" in page


@pytest.mark.django_db
def test_a_deck_tested_against_a_metagame_deck(client, user, fake_forge):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    meta = _metagame(coverage={"unresolved": [], "unsupported_cards": []})
    data = {"format": "modern", "seed": "2", "deck": str(mine.id), "opponents": [f"metagame:{meta.id}"]}
    assert client.post("/playtests/create", data).status_code == 302
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    seat = match.seats.get(position=1)
    assert seat.metagame_deck == meta and seat.deck_name == "Goblins"
    assert "Metagame · MTGTop8" in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "Metagame" in client.get(f"/playtests/{match.playtest_id}").content.decode()


@pytest.mark.django_db
def test_a_metagame_deck_of_another_format_or_failed_is_refused(client, user):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    other = _metagame("pauper")
    broken = _metagame(status="failed", error="Missing Card")
    for value, message in ((f"metagame:{other.id}", b"not a Modern metagame deck"), (f"metagame:{broken.id}", b"could not be imported")):
        data = {"format": "modern", "deck": str(mine.id), "opponents": [value]}
        response = client.post("/playtests/create", data)
        assert response.status_code == 422 and message in response.content


@pytest.mark.django_db
def test_the_deck_page_checks_the_deck_with_forge_only_on_demand(client, user, fake_forge, monkeypatch):
    from playtest import forge

    deck = make_deck(user)
    called = []
    monkeypatch.setattr(fake_forge, "check_deck", lambda fmt, text: called.append((fmt, text)) or {"unresolved": ["Bear 3"], "unsupported_cards": [], "missing_commander": False})
    page = client.get(f"/decks/{deck.id}").content.decode()
    assert "Check with Forge" in page and not called
    result = client.post(f"/decks/{deck.id}/forge-check").content.decode()
    assert called[0][0] == "commander" and called[0][1].startswith("Commander\n1 Bear Lord")
    assert "a match with this deck would be refused" in result and "Bear 3" in result

    def down(*args):
        raise forge.ForgeError("Forge bridge unreachable at http://localhost:8080")

    monkeypatch.setattr(fake_forge, "check_deck", down)
    assert "Forge bridge unreachable" in client.post(f"/decks/{deck.id}/forge-check").content.decode()


@pytest.mark.django_db
def test_someone_elses_deck_cannot_be_checked(client, django_user_model, fake_forge):
    deck = make_deck(django_user_model.objects.create_user(username="other"))
    assert client.post(f"/decks/{deck.id}/forge-check").status_code == 404


@pytest.mark.django_db
def test_a_jev_match_on_the_fake_bridge_shows_confidence_and_low_confidence_fallbacks(client, user, fake_forge, monkeypatch):
    from playtest.agents import typesafe

    from .test_jev_agent import StubJev

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", StubJev(confidence=0.9))
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks))
    match = Match.objects.get(owner=user)
    page = client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").content.decode()
    assert "0.90" in page and "runner-up:" in page

    monkeypatch.setattr(typesafe, "system_one", StubJev(confidence=0.05))
    client.post("/playtests/create", _form("duel", decks))
    unsure = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/playtests/{unsure.playtest_id}/matches/{unsure.id}").content.decode()
    assert "decided by Forge AI (delegate)" in page and "low_confidence" in page and "Jev leaned to:" in page


@pytest.mark.django_db
def test_stopping_a_match_nobody_follows_any_more_ends_it(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = make_match(user, status=Match.Status.RUNNING, forge_game_id=game["id"])
    client.post(f"/playtests/{match.playtest_id}/matches/{match.id}/stop")
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and b"hx-trigger" not in client.get(f"/playtests/{match.playtest_id}/matches/{match.id}/live").content


@pytest.mark.django_db
def test_an_unreadable_bridge_answer_leaves_no_match(client, user, monkeypatch):
    from playtest import forge

    monkeypatch.setattr(forge.ForgeClient, "create_game", lambda *a, **k: {"status": "running"})
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/playtests/create", _form("duel", decks))
    assert response.status_code == 503 and not Match.objects.exists()


@pytest.mark.django_db
def test_the_jev_model_cannot_be_set_from_the_form(client, user, fake_forge, jev):
    assert 'name="seat-0-model"' not in client.get("/playtests/new").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", decks, **{"seat-0-model": "openai/gpt-5"}))
    match = Match.objects.get(owner=user)
    assert list(match.seats.values_list("model", flat=True)) == ["", ""]
    assert {model for _, _, model in jev.requests} == {"~typesafe/jev-latest"}


def _running_session(user, fake_forge):
    playtest = Playtest.objects.create(owner=user, format="duel", deck_name="Deck 0", seed=1)
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = Match.objects.create(owner=user, playtest=playtest, seed=1, status=Match.Status.RUNNING, forge_game_id=game["id"])
    return playtest, match, game["id"]


@pytest.mark.django_db
def test_a_running_session_polls_and_can_be_stopped(client, user, fake_forge):
    playtest, match, game_id = _running_session(user, fake_forge)
    page = client.get(f"/playtests/{playtest.id}").content
    assert b"hx-trigger" in page and b"Stop all" in page
    assert client.post(f"/playtests/{playtest.id}/stop").status_code == 302
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and fake_forge.games[game_id].status == "aborted"


@pytest.mark.django_db
def test_deleting_a_session_aborts_its_games_and_deletes_its_matches(client, user, fake_forge):
    playtest, match, game_id = _running_session(user, fake_forge)
    assert client.post(f"/playtests/{playtest.id}/delete").status_code == 302
    assert not Playtest.objects.exists() and not Match.objects.exists()
    assert fake_forge.games[game_id].status == "aborted"


@pytest.mark.django_db
def test_someone_elses_session_is_a_404(client, django_user_model):
    playtest = Playtest.objects.create(owner=django_user_model.objects.create_user(username="other"), format="duel", deck_name="X", seed=1)
    for url in (f"/playtests/{playtest.id}", f"/playtests/{playtest.id}/live"):
        assert client.get(url).status_code == 404
    for url in (f"/playtests/{playtest.id}/stop", f"/playtests/{playtest.id}/delete"):
        assert client.post(url).status_code == 404


@pytest.mark.django_db
def test_sessions_are_listed_on_the_playtests_page_and_the_home_page(client, user, fake_forge):
    tested, opponent = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/playtests/create", _form("duel", [tested, opponent]))
    playtest = Playtest.objects.get()
    for url in ("/playtests", "/"):
        page = client.get(url).content.decode()
        assert f"/playtests/{playtest.id}" in page and "Deck 1" in page


@pytest.mark.django_db
def test_a_match_is_found_only_under_its_own_playtest(client, user):
    match = make_match(user)
    other = make_playtest(user)
    assert client.get(f"/playtests/{other.id}/matches/{match.id}").status_code == 404
    assert client.post(f"/playtests/{other.id}/matches/{match.id}/stop").status_code == 404
    assert client.get(f"/playtests/{match.playtest_id}/matches/{match.id}").status_code == 200


@pytest.mark.django_db
def test_the_old_match_urls_are_gone(client, user):
    match = make_match(user)
    for url in ("/matches", "/matches/new", f"/matches/{match.id}"):
        assert client.get(url).status_code == 404
