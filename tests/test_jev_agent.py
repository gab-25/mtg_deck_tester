"""Tests for the Jev agent, the agent budgets and the match summary (no network)."""

import pytest

from playtest.agents import typesafe
from playtest.agents.heuristic import HeuristicAgent
from playtest.agents.jev_agent import JevAgent, describe_option
from playtest.engine.actions import ActionKind, legal_actions
from playtest.engine.events import EventKind
from playtest.engine.game import Game
from playtest.engine.limits import Limits
from playtest.engine.state import Combat, Step
from playtest.engine.view import player_view
from playtest.summary import summarize_match

from .factories import bear, engine_deck
from .test_engine import COMMANDER, DUEL, _put, _table


class StubJev:
    """Answers like Jev: picks the option ``prefer`` says, with a given confidence."""

    def __init__(self, prefer=None, confidence=0.9, fail=None):
        self.prefer = prefer or (lambda criteria: sorted(criteria)[-1])
        self.confidence = confidence
        self.fail = fail
        self.requests = []

    def __call__(self, state, questions, model=None):
        self.requests.append((state, questions, model))
        if self.fail:
            raise typesafe.TypeSafeError(self.fail)
        criteria = questions["action"]["criteria"]
        pick = self.prefer(criteria)
        rest = (1 - self.confidence) / max(len(criteria) - 1, 1)
        probabilities = {key: (self.confidence if key == pick else rest) for key in criteria}
        return {
            "model": "typesafe/jev-1.13",
            "answers": {"action": {"type": "choice", "choice": pick, "confidence": self.confidence, "probabilities": probabilities}},
        }


def _attack_table():
    game, state = _table()
    state.step = Step.DECLARE_ATTACKERS
    state.combat = Combat("attackers")
    _put(state, bear(1, power=3))
    _put(state, bear(2), seat=1)
    return game, state


def test_the_options_are_exactly_the_legal_actions_shuffled_and_mapped_back():
    game, state = _attack_table()
    options = legal_actions(state, 0)
    view = player_view(state, 0)
    attack = next(o for o in options if o.kind == ActionKind.DECLARE_ATTACKER)
    jev = StubJev(prefer=lambda criteria: next(k for k, v in criteria.items() if v.startswith("Attack")))
    agent = JevAgent("~typesafe/jev-latest", seed=3, ask=jev)
    decision = agent.choose(view, options)
    assert decision.action == attack
    [(_, questions, model)] = jev.requests
    criteria = questions["action"]["criteria"]
    assert model == "~typesafe/jev-latest"
    assert sorted(criteria) == [f"a{i}" for i in range(len(options))]
    # The order Jev sees is a seeded shuffle, not the legal order (Pass first).
    orders = set()
    for seed in range(6):
        stub = StubJev()
        JevAgent(seed=seed, ask=stub).choose(view, options)
        orders.add(tuple(stub.requests[0][1]["action"]["criteria"]))
    assert len(orders) > 1


def test_an_option_description_carries_computed_facts():
    game, state = _attack_table()
    view = player_view(state, 0)
    attack = next(o for o in legal_actions(state, 0) if o.kind == ActionKind.DECLARE_ATTACKER)
    text = describe_option(view, attack)
    assert text.startswith(attack.label)
    assert "can be blocked by: Bear 2" in text
    assert "kills any of: Bear 2 if blocked" in text


def test_the_decision_carries_confidence_probabilities_and_model():
    game, state = _attack_table()
    decision = JevAgent(seed=1, ask=StubJev(confidence=0.82)).choose(player_view(state, 0), legal_actions(state, 0))
    assert decision.confidence == 0.82
    assert decision.model == "typesafe/jev-1.13"
    assert decision.probabilities[0][1] == 0.82
    assert not decision.reasoning and not decision.fallback_reason


@pytest.mark.parametrize(
    "jev, kind",
    [
        (StubJev(confidence=0.3), "low_confidence"),
        (StubJev(fail="HTTP 503"), "error"),
    ],
)
def test_doubt_or_failure_hands_the_decision_to_the_heuristic_agent(jev, kind):
    game, state = _attack_table()
    view, options = player_view(state, 0), legal_actions(state, 0)
    decision = JevAgent(seed=1, ask=jev, min_confidence=0.5).choose(view, options)
    assert decision.fallback_kind == kind and decision.fallback_agent == "heuristic"
    assert decision.action == HeuristicAgent().choose(view, options).action


def test_a_missing_key_falls_back(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    game, state = _attack_table()
    decision = JevAgent(seed=1).choose(player_view(state, 0), legal_actions(state, 0))
    assert decision.fallback_kind == "error"
    assert "OPENROUTER_API_KEY" in decision.fallback_reason


def _jev_game(jev, *, limits=None, clock=None, players=2):
    decks = [engine_deck(c) for c in "ABCD"[:players]]
    agents = [JevAgent(seed=i, ask=jev) for i in range(players)]
    kwargs = {"clock": clock} if clock else {}
    return Game(decks, agents, COMMANDER if players > 2 else DUEL, seed=4, limits=limits or Limits(max_rounds=30), **kwargs)


def test_a_stubbed_jev_agent_plays_a_full_game():
    result = _jev_game(StubJev()).run()
    decisions = [e for e in result.events if e.event.kind in (EventKind.DECISION, EventKind.PASS)]
    assert decisions
    payload = decisions[0].event.payload
    assert payload["confidence"] == 0.9 and payload["model"] == "typesafe/jev-1.13"
    assert payload["probabilities"][0]["p"] == 0.9


@pytest.mark.parametrize("jev", [StubJev(confidence=0.1), StubJev(fail="network down")])
def test_games_complete_through_fallbacks(jev):
    result = _jev_game(jev).run()
    assert result.events[-1].event.kind == EventKind.GAME_OVER
    fallbacks = [e for e in result.events if e.event.kind == EventKind.AGENT_FALLBACK]
    assert fallbacks and "heuristic" in fallbacks[0].event.text


def test_the_call_budget_hands_the_rest_of_the_game_to_the_heuristic_agent():
    jev = StubJev()
    result = _jev_game(jev, limits=Limits(max_rounds=30, max_agent_calls=5)).run()
    warnings = [e for e in result.events if e.event.kind == EventKind.WARNING]
    assert {e.event.seat for e in warnings} == {0, 1}
    assert len(jev.requests) == 10


def test_the_wall_clock_budget_hands_every_jev_seat_over_at_once():
    ticks = iter(range(0, 10_000, 30))
    jev = StubJev()
    result = _jev_game(jev, limits=Limits(max_rounds=30, max_wall_clock_seconds=300), clock=lambda: next(ticks), players=3).run()
    warnings = [e for e in result.events if e.event.kind == EventKind.WARNING]
    assert len(warnings) == 3 and len({e.seq for e in warnings}) == 3
    assert all("time budget" in e.event.text for e in warnings)
    assert result.events[-1].event.kind == EventKind.GAME_OVER


# -- the summary -----------------------------------------------------------------------


def test_summary_is_deterministic_and_counts_what_happened():
    def golden():
        decks = [engine_deck("A"), engine_deck("B")]
        return Game(decks, [HeuristicAgent(0), HeuristicAgent(1)], DUEL, seed=3, max_rounds=40).run()

    coverage = [{"percent": 100, "commander_unsupported": False, "dead_spells": 0}] * 2
    first, second = golden(), golden()
    summary = summarize_match(first.events, coverage, first)
    assert summary == summarize_match(second.events, coverage, second)
    assert summary["winner_seat"] == first.winner_seat
    winner = summary["seats"][first.winner_seat]
    loser = summary["seats"][1 - first.winner_seat]
    assert winner["damage_dealt"]["combat"] >= 20
    assert winner["lands_played"] > 0 and winner["spells_cast"]["creature"] > 0
    assert len(winner["life_by_round"]) == first.rounds
    assert loser["final_life"] <= 0
    assert summary["caveats"]["mana_paid_automatically"] is True


def test_summary_reports_jev_confidence_and_fallbacks():
    result = _jev_game(StubJev(confidence=0.9)).run()
    coverage = [{"percent": 90, "commander_unsupported": True, "dead_spells": 2}] * 2
    summary = summarize_match(result.events, coverage, result)
    agent = summary["seats"][0]["agent"]
    assert agent["mean_confidence"] == 0.9 and agent["rated_decisions"] > 0
    assert agent["low_confidence_fallbacks"] == 0
    assert summary["caveats"]["commander_unsupported"] == [0, 1]
    assert summary["caveats"]["lowest_coverage"] == 90
