"""Jev client: asks TypeSafe's System One model typed questions, through OpenRouter.

Jev answers ``choice`` / ``score`` / ``noul`` questions with probabilities and a
confidence instead of text. OpenRouter serves it on its own ``/systemone``
endpoint in TypeSafe's native format, so the project needs one key
(``OPENROUTER_API_KEY``) for Jev and for LLM seats alike. A thin ``requests``
wrapper rather than the official SDK keeps the project dependency-light and
lets tests stub ``requests.post``.
"""

import logging
import os
import time

import requests

from . import openrouter

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/systemone"

# An alias that moves with new releases; every answer reports the versioned
# model that produced it. Override it with JEV_MODEL.
DEFAULT_MODEL = "~typesafe/jev-latest"

# Jev answers in well under a second; a hung connection must not keep a
# background job running forever.
REQUEST_TIMEOUT = 30

# Retries on 429 (rate limited) and 529 (overloaded), with backoff.
MAX_RETRIES = 3


class TypeSafeError(Exception):
    """Jev could not be reached or gave no usable answer."""


def default_model() -> str:
    return os.environ.get("JEV_MODEL") or DEFAULT_MODEL


def system_one(
    state, questions: dict, *, model: str | None = None, key: str | None = None, sleep=time.sleep
) -> dict:
    """Asks every question about ``state`` in one request.

    Returns ``{"model": <versioned model id>, "answers": {<question id>: answer}}``.
    Raises :class:`TypeSafeError` on a missing key, a network or HTTP failure,
    or a response that doesn't answer exactly the questions asked.
    """
    key = key or openrouter.api_key()
    if not key:
        raise TypeSafeError("no OpenRouter API key configured (set OPENROUTER_API_KEY)")
    payload = {"state": state, "model": model or default_model(), "questions": questions}

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": openrouter.APP_URL,
                    "X-Title": openrouter.APP_TITLE,
                },
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise TypeSafeError(f"Jev request failed: {exc}") from exc
        if response.status_code not in (429, 529) or attempt == MAX_RETRIES:
            break
        sleep(_retry_delay(response, attempt))

    if response.status_code != 200:
        raise TypeSafeError(f"Jev returned HTTP {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
        answers = body["answers"]
        model_id = body["model"]
    except (ValueError, LookupError, TypeError) as exc:
        raise TypeSafeError(f"unexpected Jev response ({exc})") from exc
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise TypeSafeError("Jev answers don't match the questions asked")
    return {"model": model_id, "answers": answers}


def _retry_delay(response, attempt: int) -> float:
    try:
        return float(response.headers.get("retry-after"))
    except (TypeError, ValueError):
        return 0.5 * 2**attempt
