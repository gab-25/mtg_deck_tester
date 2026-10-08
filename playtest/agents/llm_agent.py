"""An agent that asks a language model which action to take."""

import json
import logging
import re

from ..engine.actions import Action
from ..engine.rules import __doc__ as RULES_TEXT
from . import openrouter
from .base import Decision
from .random_agent import RandomAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are playing a game of Magic: The Gathering \
(Commander) against other players. Play to win: develop your mana, deploy \
threats, and attack the opponent it pays most to attack.

The engine enforces these rules:
{RULES_TEXT}
Every message gives you the game state as JSON and a numbered list of the \
actions you may take. Reply with a single JSON object and nothing else:
{{"choice": <number of the action>, "reason": "<one short sentence>"}}"""

# The first {...} block in the reply: models often wrap JSON in prose or fences.
_JSON_OBJECT = re.compile(r"\{.*?\}", re.S)


def build_messages(view: dict, options: list[Action]) -> list[dict]:
    numbered = "\n".join(f"{i}. {action.label}" for i, action in enumerate(options, start=1))
    user = f"Game state:\n{json.dumps(view, indent=1)}\n\nYour options:\n{numbered}"
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
    """Plays through ``chat`` (OpenRouter by default); never blocks the game.

    Any failure — no key, a network error, a reply that names no valid option —
    is handed to ``fallback`` and reported on the decision, so the game goes on.
    """

    def __init__(self, model: str | None = None, *, seed: int | None = None, chat=None):
        self.model = model or openrouter.default_model()
        self.chat = chat or openrouter.chat
        self.fallback = RandomAgent(seed)

    def choose(self, view: dict, options: list[Action]) -> Decision:
        try:
            reply = self.chat(build_messages(view, options), model=self.model)
            index, reason = parse_choice(reply, len(options))
        except (openrouter.ChatError, ValueError) as exc:
            logger.warning("LLM agent (%s) fell back to random: %s", self.model, exc)
            fallback = self.fallback.choose(view, options)
            return Decision(fallback.action, fallback_reason=str(exc))
        return Decision(options[index], reasoning=reason)
