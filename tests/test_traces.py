"""The decision trace: what Jev saw and answered, kept for every decision it was asked (#72)."""

import pytest

from playtest import runner, workers
from playtest.agents import typesafe
from playtest.agents.base import Decision, delegate
from playtest.models import DecisionTrace, Match, Seat

from .factories import make_constructed_deck, make_match, make_playtest
from .test_jev_agent import StubJev


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="u")


def test_meta_carries_the_trace_id_but_never_the_trace():
    answer = Decision(1, confidence=0.9, model="m", trace={"state": {"big": True}}, trace_id=7)
    assert answer.meta() == {"confidence": 0.9, "model": "m", "trace": 7}
    assert "trace" not in Decision(1, trace={"state": {}}).meta()
    assert delegate("error", "x", trace={"a": 1}).trace == {"a": 1}


@pytest.mark.django_db
def test_deleting_a_match_deletes_its_traces(user):
    match = make_match(user)
    DecisionTrace.objects.create(match=match, seat_position=0, kind="priority")
    match.delete()
    assert not DecisionTrace.objects.exists()


@pytest.fixture
def jev(monkeypatch):
    stub = StubJev(confidence=0.9)
    monkeypatch.setattr(typesafe, "system_one", stub)
    return stub


def _played(user, agents=("jev", "jev"), seed=5):
    match = Match.objects.create(owner=user, playtest=make_playtest(user, fmt="modern", seed=seed), format="modern", seed=seed)
    for position, agent in enumerate(agents):
        deck = make_constructed_deck(user, name=f"Deck {position}")
        Seat.objects.create(match=match, position=position, deck=deck, deck_name=deck.name, agent=agent)
    runner.run_match(match.id)
    match.refresh_from_db()
    return match


@pytest.mark.django_db
def test_every_jev_decision_has_a_trace_linked_to_its_event(user, fake_forge, jev):
    match = _played(user)
    assert match.status == Match.Status.FINISHED, match.error
    decisions = list(match.events.filter(kind="decision"))
    traces = list(match.traces.all())
    assert len(traces) == len(decisions) == len(jev.requests)
    for trace, event in zip(traces, decisions):
        assert trace.event_id == event.id and event.payload["trace"] == trace.id
        assert trace.choice == event.payload["choice"] and trace.kind == event.payload["kind"]
        assert trace.seat_position == event.seat_position and trace.state["you"]["life"]
        assert trace.outcome == "done" and trace.model == "typesafe/jev-1.13"
    sent = fake_forge.answers[0]["meta"]
    assert set(sent) == {"confidence", "probabilities", "model", "trace"}


@pytest.mark.django_db
def test_a_delegated_decision_is_traced_as_delegated(user, fake_forge, jev):
    jev.confidence = 0.05
    match = _played(user, agents=("jev", "forge_ai"))
    traces = list(match.traces.all())
    assert traces and all(t.outcome == "delegated" and t.choice is None for t in traces)
    assert all(t.fallback_kind == "low_confidence" and t.event.kind == "fallback" for t in traces)


@pytest.mark.django_db
def test_an_unreachable_jev_still_leaves_a_trace(user, fake_forge, jev):
    jev.fail = "down"
    match = _played(user)
    trace = match.traces.first()
    assert trace.fallback_kind == "error" and trace.options and trace.probabilities == {}
    assert trace.outcome == "delegated"


@pytest.mark.django_db
def test_a_play_forge_did_not_carry_out_is_traced_as_no_effect(user, fake_forge, jev):
    fake_forge.failing_cast = True
    match = _played(user)
    no_effect = list(match.traces.filter(outcome="no_effect"))
    assert no_effect and all("Cast Stifle" in t.chosen_label for t in no_effect)
    retried = match.traces.exclude(failed_play="")
    assert retried.count() == len(no_effect)


@pytest.mark.django_db
def test_no_trace_when_jev_is_not_asked(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "0")
    match = _played(user)
    assert not jev.requests and not match.traces.exists()


@pytest.mark.django_db
def test_a_trace_that_cannot_be_stored_does_not_cost_the_decision(user, fake_forge, jev, monkeypatch):
    from django.db import DatabaseError

    def broken(**kwargs):
        raise DatabaseError("disk full")

    monkeypatch.setattr(workers.DecisionTrace.objects, "create", broken)
    match = _played(user)
    assert match.status == Match.Status.FINISHED, match.error
    assert match.events.filter(kind="decision").exists() and not match.traces.exists()
    assert all("trace" not in a["meta"] for a in fake_forge.answers)
