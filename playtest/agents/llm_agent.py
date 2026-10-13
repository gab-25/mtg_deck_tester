"""An agent that asks a language model which of Forge's options to take.

Whether it stays is decided with measurements in #44; until then it plays
behind the same interface as Jev: options in, a choice and a reason out, and
any failure delegates to Forge's AI.
"""

import json
import logging
import re

from . import openrouter
from .base import Decision, delegate
from .jev_agent import KIND_LINES, describe_option, jev_state

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are playing a game of Magic: The Gathering against one \
opponent. Play to win: develop your mana, deploy threats, remove the \
opponent's best creatures and attack when it pays.

The Forge engine enforces the full rules: it lists only legal options, pays \
mana and assigns combat damage for you. Every message gives you the game state \
as JSON, what Forge asks, and the numbered options, each with facts Forge \
computed for it. Reply with a single JSON object and nothing else:
{"choice": <number of the option>, "reason": "<one short sentence>"}"""

# The first {...} block in the reply: models often wrap JSON in prose or fences.
_JSON_OBJECT = re.compile(r"\{.*?\}", re.S)


def build_messages(view: dict, options: list[dict], *, kind: str = "", prompt: str = "") -> list[dict]:
    numbered = "\n".join(f"{i}. {describe_option(option)}" for i, option in enumerate(options, start=1))
    user = (
        f"Format: {view.get('format', '')}\n"
        f"Game state:\n{jev_state(view, kind=kind, prompt=prompt)}\n\n"
        f"{KIND_LINES.get(kind, '')} Forge asks: {prompt}\n\nYour options:\n{numbered}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_choice(reply: str, count: int) -> tuple[int, str]:
    """Reads ``(index, reason)`` off a model reply; the index is 0-based.

    Raises ``ValueError`` when the reply holds no valid choice.
    """
    match = _JSON_OBJECT.search(reply)
    if not match:
        raise ValueError("no JSON object in the reply")
    data = json.loads(match.group(0))
    choice = data.get("choice")
    if isinstance(choice, str) and choice.strip().isdigit():
        choice = int(choice)
    if not isinstance(choice, int) or isinstance(choice, bool) or not 1 <= choice <= count:
        raise ValueError(f"choice {choice!r} is not between 1 and {count}")
    return choice - 1, str(data.get("reason") or "").strip()


class LLMAgent:
    """Plays through ``chat`` (OpenRouter by default); never blocks the game."""

    # Model-backed: the worker pool meters its calls and the match's wall clock.
    metered = True

    def __init__(self, model: str | None = None, *, seed: int | None = None, chat=None):
        self.model = model or openrouter.default_model()
        self.chat = chat or openrouter.chat

    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "") -> Decision:
        try:
            reply = self.chat(build_messages(view, options, kind=kind, prompt=prompt), model=self.model)
            index, reason = parse_choice(reply, len(options))
        except (openrouter.ChatError, ValueError) as exc:
            logger.warning("LLM agent (%s) delegated to Forge's AI: %s", self.model, exc)
            return delegate("error", str(exc), model=self.model)
        return Decision(options[index]["index"], reasoning=reason, model=self.model)
