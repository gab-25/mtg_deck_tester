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
    data = {"format": fmt, "seed": "7"}
    for i, deck in enumerate(decks):
        data[f"seat-{i}-deck"] = str(deck.id)
        data[f"seat-{i}-agent"] = agent
    data.update(extra)
    return data


@pytest.mark.django_db
def test_new_match_form_lists_my_ready_decks(client, user):
    make_deck(user, name="Ready Bears", fmt="duel")
    make_deck(user, name="Still Importing", fmt="duel", status="processing")
    content = client.get("/matches/new").content
    assert b"Ready Bears" in content
    assert b"Still Importing" not in content


@pytest.mark.django_db
def test_new_match_form_prefills_the_deck_from_its_page(client, user):
    deck = make_deck(user, fmt="duel")
    content = client.get(f"/matches/new?deck={deck.id}&format=duel").content.decode()
    assert f'value="{deck.id}" data-format="duel" selected' in content


@pytest.mark.django_db
def test_a_duel_between_random_agents_runs_to_the_end(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks))
    match = Match.objects.get(owner=user)
    assert response.status_code == 302
    assert response["Location"] == f"/matches/{match.id}"
    assert match.status == Match.Status.FINISHED, match.error
    assert match.seed == 7 and fake_forge.created[0]["seed"] == 7

    page = client.get(f"/matches/{match.id}").content.decode()
    assert "Game log" in page and "Turn 1 (Deck 0)" in page and "played by Forge" in page
    # Every step of every turn stays out of the page.
    assert "{T}: Add {R}" not in page
    # The finished region no longer polls.
    assert b"hx-trigger" not in client.get(f"/matches/{match.id}/live").content


@pytest.mark.django_db
def test_multiplayer_commander_cannot_be_played_yet(client, user, fake_forge):
    decks = [make_deck(user, name=f"Deck {i}") for i in range(2)]
    response = client.post("/matches/create", _form("commander", decks))
    assert response.status_code == 422
    assert b"Commander cannot be played yet" in response.content
    assert not Match.objects.exists() and not fake_forge.created
    assert b'<option value="commander"' not in client.get("/matches/new").content


@pytest.mark.django_db
def test_the_same_deck_can_sit_twice(client, user, fake_forge):
    deck = make_deck(user, fmt="duel")
    client.post("/matches/create", _form("duel", [deck, deck]))
    match = Match.objects.get(owner=user)
    assert match.seats.count() == 2
    # Forge tells the two apart.
    assert [s["name"] for s in fake_forge.created[0]["seats"]] == ["Bears (seat 1)", "Bears (seat 2)"]


@pytest.mark.django_db
def test_the_seat_count_must_fit_the_format(client, user):
    decks = [make_deck(user, name="Deck 0", fmt="duel")]
    response = client.post("/matches/create", _form("duel", decks))
    assert response.status_code == 422
    assert b"exactly 2 players; 1 chosen" in response.content
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
@pytest.mark.parametrize("agent", ["llm", "jev"])
def test_model_seats_need_an_api_key(client, user, agent):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks, agent=agent))
    assert response.status_code == 422
    assert b"OPENROUTER_API_KEY" in response.content


@pytest.mark.django_db
def test_an_llm_match_records_the_reasoning(client, user, monkeypatch, fake_forge):
    from playtest.agents import llm_agent

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_agent.openrouter, "chat", lambda messages, model: '{"choice": 1, "reason": "first is best"}'
    )
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/matches/create", _form("duel", decks, agent="llm"))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    assert match.events.filter(reasoning="first is best").exists()
    assert b"first is best" in client.get(f"/matches/{match.id}").content


@pytest.mark.django_db
def test_a_deck_forge_does_not_know_is_refused_naming_the_card(client, user, fake_forge):
    fake_forge.unknown = {"Bear 7"}
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks))
    assert response.status_code == 422
    assert "Seat 1 (Deck 0): Forge does not know Bear 7." in response.content.decode()
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_an_unreachable_bridge_is_reported_and_no_match_is_left(client, user, monkeypatch):
    from playtest import forge

    def down(*args, **kwargs):
        raise forge.ForgeError("Forge bridge unreachable at http://localhost:8080: refused")

    monkeypatch.setattr(forge.ForgeClient, "create_game", down)
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks))
    assert response.status_code == 503
    assert b"Forge bridge unreachable" in response.content
    assert not Match.objects.exists()


@pytest.mark.django_db
def test_bad_numbers_are_reported(client, user):
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks, seed="abc"))
    assert response.status_code == 422
    assert b"seed must be a whole number" in response.content


@pytest.mark.django_db
def test_someone_elses_match_is_a_404(client, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    match = Match.objects.create(owner=other, seed=1)
    assert client.get(f"/matches/{match.id}").status_code == 404
    assert client.get(f"/matches/{match.id}/live").status_code == 404
    assert client.post(f"/matches/{match.id}/stop").status_code == 404
    assert client.post(f"/matches/{match.id}/delete").status_code == 404


@pytest.mark.django_db
def test_a_running_match_polls_its_live_region_and_can_be_stopped(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = Match.objects.create(owner=user, seed=1, status=Match.Status.RUNNING, forge_game_id=game["id"])
    page = client.get(f"/matches/{match.id}").content
    assert b"hx-trigger" in page and b"Stop" in page
    assert client.post(f"/matches/{match.id}/stop").status_code == 302
    assert fake_forge.games[game["id"]].status == "aborted"


@pytest.mark.django_db
def test_deleting_a_running_match_aborts_its_game(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = Match.objects.create(owner=user, seed=1, status=Match.Status.RUNNING, forge_game_id=game["id"])
    assert client.post(f"/matches/{match.id}/delete").status_code == 302
    assert not Match.objects.exists()
    assert fake_forge.games[game["id"]].status == "aborted"


# -- coverage, confidence and the summary ---------------------------------------------


@pytest.mark.django_db
def test_the_form_offers_the_forge_agents(client):
    content = client.get("/matches/new").content.decode()
    for agent in ("forge_ai", "jev", "llm", "random"):
        assert f'value="{agent}"' in content
    assert 'value="heuristic"' not in content


def _finished(user, **seat_fields):
    from playtest.models import MatchEvent, Seat

    match = Match.objects.create(owner=user, format="duel", seed=1, status=Match.Status.FINISHED, turn=3, end_reason="turn_limit")
    for position in range(2):
        fields = {"deck_name": f"Deck {position}", "agent": "jev", **(seat_fields if position == 0 else {})}
        Seat.objects.create(match=match, position=position, **fields)
    return match, MatchEvent


@pytest.mark.django_db
def test_seat_cards_show_forges_coverage(client, user):
    match, _ = _finished(user, coverage={"unsupported_cards": ["Odd Card"]})
    match.seats.filter(position=1).update(coverage={"unsupported_cards": []})
    page = client.get(f"/matches/{match.id}").content.decode()
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
    page = client.get(f"/matches/{match.id}").content.decode()
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
    assert "Match summary" not in client.get(f"/matches/{match.id}").content.decode()
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    client.post("/matches/create", _form("duel", decks))
    played = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/matches/{played.id}").content.decode()
    assert "Match summary" in page and "Spells cast" in page and "Fallbacks" in page
    assert "Automatic for every seat: mana payment" in page and "assignCombatDamage" in page
    assert "% Random" in page


@pytest.mark.django_db
def test_a_constructed_match_between_two_decks(client, user, fake_forge):
    from .factories import make_constructed_deck

    decks = [make_constructed_deck(user, name=f"Goblins {i}") for i in range(2)]
    client.post("/matches/create", _form("modern", decks, agent="forge_ai"))
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    assert [s["controller"] for s in fake_forge.created[0]["seats"]] == ["forge_ai", "forge_ai"]
    assert "all Forge AI" in client.get(f"/matches/{match.id}").content.decode()


@pytest.mark.django_db
def test_the_new_match_form_tags_decks_by_format(client, user):
    from .factories import make_constructed_deck

    make_constructed_deck(user, name="Goblins")
    page = client.get("/matches/new").content.decode()
    assert 'data-format="modern"' in page
    assert "benchmark" not in page


@pytest.mark.django_db
def test_hidden_seats_and_decks_are_cleared_not_just_hidden(client):
    page = client.get("/matches/new").content.decode()
    # Hidden rows and options must not stay selected and be posted unseen.
    assert 'select.value = ""' in page


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
    page = client.get("/matches/new").content.decode()
    assert f'value="metagame:{deck.id}" data-format="modern"' in page
    assert "Goblins — 8.4% — 1 card unknown to Forge ⚠" in page


@pytest.mark.django_db
def test_a_match_against_a_metagame_deck(client, user, fake_forge):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    meta = _metagame()
    data = {"format": "modern", "seed": "2", "seat-0-deck": str(mine.id), "seat-0-agent": "random",
            "seat-1-deck": f"metagame:{meta.id}", "seat-1-agent": "forge_ai"}
    assert client.post("/matches/create", data).status_code == 302
    match = Match.objects.get(owner=user)
    assert match.status == Match.Status.FINISHED, match.error
    seat = match.seats.get(position=1)
    assert seat.metagame_deck == meta and seat.deck_name == "Goblins"
    assert "Metagame · MTGTop8" in client.get(f"/matches/{match.id}").content.decode()


@pytest.mark.django_db
def test_a_metagame_deck_of_another_format_or_failed_is_refused(client, user):
    from .factories import make_constructed_deck

    mine = make_constructed_deck(user)
    other = _metagame("pauper")
    broken = _metagame(status="failed", error="Missing Card")
    for value, message in ((f"metagame:{other.id}", b"not a Modern metagame deck"), (f"metagame:{broken.id}", b"could not be imported")):
        data = {"format": "modern", "seat-0-deck": str(mine.id), "seat-1-deck": value}
        response = client.post("/matches/create", data)
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
    client.post("/matches/create", _form("duel", decks, agent="jev"))
    match = Match.objects.get(owner=user)
    page = client.get(f"/matches/{match.id}").content.decode()
    assert "0.90" in page and "runner-up:" in page

    monkeypatch.setattr(typesafe, "system_one", StubJev(confidence=0.05))
    client.post("/matches/create", _form("duel", decks, agent="jev"))
    unsure = Match.objects.exclude(pk=match.pk).get()
    page = client.get(f"/matches/{unsure.id}").content.decode()
    assert "decided by Forge AI (delegate)" in page and "low_confidence" in page and "Jev leaned to:" in page


@pytest.mark.django_db
def test_stopping_a_match_nobody_follows_any_more_ends_it(client, user, fake_forge):
    game = fake_forge.create_game("duel", 1, [{"name": "A", "decklist": "1 Forest"}, {"name": "B", "decklist": "1 Forest"}])
    match = Match.objects.create(owner=user, seed=1, status=Match.Status.RUNNING, forge_game_id=game["id"])
    client.post(f"/matches/{match.id}/stop")
    match.refresh_from_db()
    assert match.status == Match.Status.STOPPED and b"hx-trigger" not in client.get(f"/matches/{match.id}/live").content


@pytest.mark.django_db
def test_an_unreadable_bridge_answer_leaves_no_match(client, user, monkeypatch):
    from playtest import forge

    monkeypatch.setattr(forge.ForgeClient, "create_game", lambda *a, **k: {"status": "running"})
    decks = [make_deck(user, name=f"Deck {i}", fmt="duel") for i in range(2)]
    response = client.post("/matches/create", _form("duel", decks))
    assert response.status_code == 503 and not Match.objects.exists()
