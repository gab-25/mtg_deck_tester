"""Tests for the agent interface and the random agent."""

from playtest.agents.base import Decision, delegate
from playtest.agents.random_agent import RandomAgent

from .fakeforge import sample_decision

DECISION = sample_decision()
VIEW, OPTIONS = DECISION["view"], DECISION["options"]


def test_random_agent_is_reproducible_and_picks_an_offered_option():
    picks = lambda seed: [RandomAgent(seed).choose(VIEW, OPTIONS).choice for _ in range(5)]  # noqa: E731
    assert picks(3) == picks(3)
    assert all(0 <= pick < len(OPTIONS) for pick in picks(3))


def test_a_decision_is_a_choice_or_a_delegation_with_its_meta():
    assert not Decision(2).delegate and Decision(2).meta() == {}
    handed = delegate("low_confidence", "unsure", confidence=0.12345, probabilities=(("Pass priority", 0.5),), model="m")
    assert handed.delegate
    assert handed.meta() == {
        "confidence": 0.1235,
        "probabilities": [{"label": "Pass priority", "p": 0.5}],
        "model": "m",
        "fallback_reason": "unsure",
        "fallback_kind": "low_confidence",
    }

