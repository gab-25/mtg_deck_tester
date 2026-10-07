"""Tests for the Jev client (TypeSafe System One through OpenRouter; no network)."""

import pytest
import requests

from playtest.agents import typesafe

QUESTIONS = {"op": {"type": "choice", "instructions": "Which?", "criteria": {"A": None, "B": None}}}
ANSWER = {
    "type": "choice",
    "choice": "A",
    "probabilities": {"A": 0.9, "B": 0.1},
    "confidence": 0.85,
}


class _Response:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok():
    # OpenRouter adds id, provider and usage.cost to TypeSafe's response.
    return _Response(
        payload={
            "id": "gen-1",
            "provider": "TypeSafe",
            "model": "typesafe/jev-1.13",
            "answers": {"op": ANSWER},
            "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.00001},
        }
    )


def test_requires_an_openrouter_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(typesafe.TypeSafeError, match="OPENROUTER_API_KEY"):
        typesafe.system_one("state", QUESTIONS)


def test_uses_the_openrouter_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(headers=headers)
        return _ok()

    monkeypatch.setattr(typesafe.requests, "post", post)
    typesafe.system_one("state", QUESTIONS)
    assert sent["headers"]["Authorization"] == "Bearer env-key"


def test_sends_state_model_and_questions_and_returns_the_answers(monkeypatch):
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)
        return _ok()

    monkeypatch.setattr(typesafe.requests, "post", post)
    result = typesafe.system_one({"card": "Bolt"}, QUESTIONS, key="k")
    assert result == {"model": "typesafe/jev-1.13", "answers": {"op": ANSWER}}
    assert sent["url"] == "https://openrouter.ai/api/v1/systemone"
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["headers"]["X-Title"] == "mtg_deck_tester"
    assert sent["json"] == {
        "state": {"card": "Bolt"},
        "model": "~typesafe/jev-latest",
        "questions": QUESTIONS,
    }
    assert sent["timeout"] == typesafe.REQUEST_TIMEOUT


def test_default_model_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("JEV_MODEL", "typesafe/jev-1.13")
    assert typesafe.default_model() == "typesafe/jev-1.13"
    monkeypatch.delenv("JEV_MODEL")
    assert typesafe.default_model() == typesafe.DEFAULT_MODEL


def test_retries_rate_limits_with_backoff_then_succeeds(monkeypatch):
    replies = [_Response(429, headers={"retry-after": "2"}), _Response(529), _ok()]
    monkeypatch.setattr(typesafe.requests, "post", lambda *a, **k: replies.pop(0))
    waits = []
    result = typesafe.system_one("state", QUESTIONS, key="k", sleep=waits.append)
    assert result["answers"] == {"op": ANSWER}
    assert waits == [2.0, 1.0]


def test_gives_up_after_the_last_retry(monkeypatch):
    monkeypatch.setattr(
        typesafe.requests, "post", lambda *a, **k: _Response(429, text="slow down")
    )
    waits = []
    with pytest.raises(typesafe.TypeSafeError, match="HTTP 429"):
        typesafe.system_one("state", QUESTIONS, key="k", sleep=waits.append)
    assert len(waits) == typesafe.MAX_RETRIES


@pytest.mark.parametrize(
    "response, message",
    [
        (_Response(401, text="bad key"), "HTTP 401"),
        (_Response(200), "unexpected"),
        (_Response(200, payload={"model": "typesafe/jev-1.13", "answers": {}}), "don't match"),
    ],
)
def test_bad_responses_raise(monkeypatch, response, message):
    monkeypatch.setattr(typesafe.requests, "post", lambda *a, **k: response)
    with pytest.raises(typesafe.TypeSafeError, match=message):
        typesafe.system_one("state", QUESTIONS, key="k")


def test_network_errors_raise(monkeypatch):
    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(typesafe.requests, "post", offline)
    with pytest.raises(typesafe.TypeSafeError, match="offline"):
        typesafe.system_one("state", QUESTIONS, key="k")
