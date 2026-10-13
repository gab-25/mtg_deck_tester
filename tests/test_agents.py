"""Tests for the agent interface."""

from playtest.agents.base import Decision, delegate


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

