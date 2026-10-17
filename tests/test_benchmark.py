"""The Jev benchmark (#79): Jev against Forge's AI with a match's decks, and the report."""

import pytest
from django.core.management import CommandError, call_command

from playtest import benchmark
from playtest.agents import typesafe
from playtest.models import Match, Playtest

from .factories import make_constructed_deck, make_match
from .test_jev_agent import StubJev


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


@pytest.fixture
def source(user):
    match = make_match(user, seats=("Goblins", "Elves"), format="modern")
    for seat, name in zip(match.seats.all(), ("Goblins", "Elves")):
        seat.deck = make_constructed_deck(user, name=name)
        seat.save()
    return match


@pytest.mark.django_db
def test_jev_plays_each_deck_against_forge_ai_on_the_same_seeds(source):
    playtests = benchmark.create_benchmark(source, games=3, seed=40)
    assert [p.deck_name for p in playtests] == ["Goblins", "Elves"]
    for playtest, (jev, other) in zip(playtests, [("Goblins", "Elves"), ("Elves", "Goblins")]):
        assert playtest.games_per_opponent == 3 and playtest.seed == 40 and playtest.deck.name == jev
        matches = list(playtest.matches.order_by("seed"))
        assert [m.seed for m in matches] == [40, 41, 42]
        for match in matches:
            assert [(s.deck.name, s.agent) for s in match.seats.order_by("position")] == [(jev, "jev"), (other, "forge_ai")]
    assert [m.playtest_id for m in benchmark.queue_order(playtests)[:2]] == [p.id for p in playtests]


@pytest.mark.django_db
def test_a_control_benchmark_plays_the_same_games_without_jev(source, fake_forge, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    call_command("benchmark", str(source.id), games=1, seed=7, control=True)
    assert "Forge's AI plays Goblins" in capsys.readouterr().out
    matches = Match.objects.exclude(playtest_id=source.playtest_id)
    assert {s.agent for m in matches for s in m.seats.all()} == {"forge_ai"}
    assert sorted(m.seed for m in matches) == [7, 7] and all(m.status == "finished" for m in matches)
    assert not matches.filter(traces__isnull=False).exists()


@pytest.mark.django_db
def test_a_benchmark_needs_two_seats(user):
    match = make_match(user, seats=("Only",))
    with pytest.raises(ValueError):
        benchmark.create_benchmark(match, games=1, seed=1)


def _summary(result_seat, flags=None, agreement=None, confidence=0.4):
    jev = {"seat": 0, "decisions": {"agent": 9, "forge_ai": 1}, "fallbacks": {"low_confidence": 1},
           "confidence": {"median": confidence}, "flags": flags or {}}
    if agreement:
        jev["agreement"] = agreement
    return {"turns": 10, "seats": [jev, {"seat": 1}], "winner_seat": result_seat}


@pytest.mark.django_db
def test_the_report_counts_jevs_seat_over_finished_matches(source):
    playtest = Playtest.objects.create(owner=source.owner, format="modern", deck_name="Goblins", seed=1)
    won = make_match(source.owner, playtest=playtest, status="finished", summary=_summary(
        0, flags={"bad_attack": 2}, agreement={"rated": 4, "agree": 3, "by_kind": {"attack": [4, 3]}}, confidence=0.3))
    won.winner = won.seats.get(position=0)
    won.save()
    lost = make_match(source.owner, playtest=playtest, status="finished", summary=_summary(1, confidence=0.5))
    lost.winner = lost.seats.get(position=1)
    lost.save()
    make_match(source.owner, playtest=playtest, status="running")
    result = benchmark.report("x", Match.objects.filter(playtest=playtest))
    assert (result.games, result.unfinished, result.won, result.lost) == (2, 1, 1, 1)
    assert result.win_rate == 50 and result.jev_share == 90 and result.median_confidence == 0.4
    assert result.flags_per_game == 1.0 and result.agreement == 75 and result.by_kind == {"attack": [4, 3]}
    text = benchmark.table([result])
    assert "Jev win rate" in text and "50%" in text and "flag: bad_attack" in text and "75% of 4" in text


@pytest.mark.django_db
def test_the_commands_play_and_report_a_benchmark(source, fake_forge, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(typesafe, "system_one", StubJev(confidence=0.9))
    call_command("benchmark", str(source.id), games=1, seed=7)
    out = capsys.readouterr().out
    playtests = list(Playtest.objects.exclude(pk=source.playtest_id).order_by("deck_name"))
    assert len(playtests) == 2 and "Seed 7" in out
    assert all(m.status == "finished" for p in playtests for m in p.matches.all())
    call_command("benchmark_report", *[str(p.id) for p in playtests])
    out = capsys.readouterr().out
    assert "Jev won–lost–draw" in out and "All" in out


@pytest.mark.django_db
def test_the_benchmark_needs_an_api_key(source, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(CommandError, match="OPENROUTER_API_KEY"):
        call_command("benchmark", str(source.id))
