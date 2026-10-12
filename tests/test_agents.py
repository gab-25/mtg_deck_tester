"""Tests for the agents and the OpenRouter client (no network)."""

import pytest
import requests

from playtest.agents import openrouter
from playtest.agents.llm_agent import LLMAgent, build_messages, parse_choice
from playtest.agents.random_agent import RandomAgent
from playtest.engine.actions import Action, ActionKind

OPTIONS = [
    Action(ActionKind.PLAY_LAND, card=1, label="Play land Forest"),
    Action(ActionKind.CAST, card=2, label="Cast Bear (mana value 2)"),
    Action(ActionKind.PASS, label="End your main phase"),
]
VIEW = {"round": 1, "you": {"life": 40}, "opponents": []}


def test_random_agent_is_reproducible():
    picks = lambda seed: [RandomAgent(seed).choose(VIEW, OPTIONS).action for _ in range(5)]  # noqa: E731
    assert picks(3) == picks(3)


def test_messages_number_the_options():
    [system, user] = build_messages(VIEW, OPTIONS)
    assert "Commander" in system["content"]
    assert "1. Play land Forest" in user["content"]
    assert "3. End your main phase" in user["content"]


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
    agent = LLMAgent("some/model", chat=lambda messages, model: '{"choice": 2, "reason": "develop"}')
    decision = agent.choose(VIEW, OPTIONS)
    assert decision.action == OPTIONS[1]
    assert decision.reasoning == "develop"
    assert not decision.fallback_reason


def test_llm_agent_falls_back_on_a_bad_reply():
    agent = LLMAgent("some/model", seed=1, chat=lambda messages, model: "no idea")
    decision = agent.choose(VIEW, OPTIONS)
    assert decision.action in OPTIONS
    assert "JSON" in decision.fallback_reason


def test_llm_agent_falls_back_when_the_model_is_unreachable():
    def down(messages, model):
        raise openrouter.ChatError("HTTP 503")

    decision = LLMAgent("some/model", seed=1, chat=down).choose(VIEW, OPTIONS)
    assert decision.action in OPTIONS
    assert decision.fallback_reason == "HTTP 503"


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
