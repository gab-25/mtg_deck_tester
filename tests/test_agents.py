"""Tests for the agents and the OpenRouter client (no network)."""

import pytest
import requests

from playtest.agents import openrouter
from playtest.agents.base import Decision, delegate
from playtest.agents.llm_agent import LLMAgent, build_messages, parse_choice
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


def test_messages_number_the_options_with_their_facts():
    [system, user] = build_messages(VIEW, OPTIONS, kind="attack", prompt="Declare attackers")
    assert "Forge engine" in system["content"]
    assert "1. Done (no more attackers)" in user["content"]
    assert "2. Attack with Bear #3 (3/3) -> Green — deals 3 damage if unblocked" in user["content"]
    assert "Declare attackers" in user["content"]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"choice": 2, "reason": "curve out"}', (1, "curve out")),
        ('Sure!\n```json\n{"choice": "1", "reason": "land"}\n```', (0, "land")),
        ('{"choice": 3}', (2, "")),
    ],
)
def test_parse_choice(reply, expected):
    assert parse_choice(reply, 3) == expected


@pytest.mark.parametrize("reply", ["I cast the bear.", '{"choice": 4}', '{"choice": 0}', '{"choice": true}'])
def test_parse_choice_rejects_invalid_replies(reply):
    with pytest.raises(ValueError):
        parse_choice(reply, 3)


def test_llm_agent_plays_the_models_choice():
    agent = LLMAgent("some/model", chat=lambda messages, model: '{"choice": 2, "reason": "attack"}')
    decision = agent.choose(VIEW, OPTIONS, kind="attack", prompt="Declare attackers")
    assert decision.choice == OPTIONS[1]["index"]
    assert decision.reasoning == "attack" and decision.meta()["reason"] == "attack"
    assert not decision.delegate


def test_llm_agent_delegates_on_a_bad_reply():
    decision = LLMAgent("some/model", chat=lambda messages, model: "no idea").choose(VIEW, OPTIONS)
    assert decision.delegate and decision.fallback_kind == "error"
    assert "JSON" in decision.fallback_reason


def test_llm_agent_delegates_when_the_model_is_unreachable():
    def down(messages, model):
        raise openrouter.ChatError("HTTP 503")

    decision = LLMAgent("some/model", chat=down).choose(VIEW, OPTIONS)
    assert decision.delegate and decision.fallback_reason == "HTTP 503"


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_chat_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(openrouter.ChatError, match="API key"):
        openrouter.chat([{"role": "user", "content": "hi"}])


def test_chat_returns_the_completion(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json)
        return _Response(payload={"choices": [{"message": {"content": "hello"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    assert openrouter.chat([{"role": "user", "content": "hi"}], model="x/y", key="k") == "hello"
    assert sent["json"]["model"] == "x/y"
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["headers"]["X-Title"] == "mtg_deck_tester"


def test_chat_uses_the_configured_default_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
    assert openrouter.default_model() == "anthropic/claude-sonnet-5"
    monkeypatch.delenv("OPENROUTER_MODEL")
    assert openrouter.default_model() == openrouter.DEFAULT_MODEL


@pytest.mark.parametrize(
    "response, message",
    [
        (_Response(500, text="boom"), "HTTP 500"),
        (_Response(200, payload={"choices": []}), "unexpected"),
        (_Response(200, payload={"choices": [{"message": {"content": "  "}}]}), "empty"),
    ],
)
def test_chat_reports_failures(monkeypatch, response, message):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(openrouter.ChatError, match=message):
        openrouter.chat([{"role": "user", "content": "hi"}], key="k")


def test_chat_reports_network_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(openrouter.ChatError, match="offline"):
        openrouter.chat([{"role": "user", "content": "hi"}], key="k")
