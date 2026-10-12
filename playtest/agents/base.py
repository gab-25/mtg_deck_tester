"""The interface between the engine and whoever plays a seat."""

from dataclasses import dataclass
from typing import Protocol

from ..engine.actions import Action


@dataclass(frozen=True)
class Decision:
    action: Action
    # Why the agent chose it, in its own words (empty for agents that don't say).
    reasoning: str = ""
    # Set when the agent could not decide on its own and a fallback chose instead.
    fallback_reason: str = ""
    # Which agent decided instead ("heuristic", "random"), and why:
    # "error" (unreachable, bad answer) or "low_confidence".
    fallback_agent: str = ""
    fallback_kind: str = ""
    # For a model that answers with a distribution (Jev): how sure it was, the
    # top actions with their probability, and the versioned model id.
    confidence: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()
    model: str = ""

    def payload(self) -> dict:
        """The JSON-safe part of the decision a match event stores."""
        data = {}
        if self.confidence is not None:
            data["confidence"] = round(self.confidence, 4)
        if self.probabilities:
            data["probabilities"] = [{"label": label, "p": round(p, 4)} for label, p in self.probabilities]
        if self.model:
            data["model"] = self.model
        if self.fallback_reason:
            data["fallback_reason"] = self.fallback_reason
        if self.fallback_agent:
            data["fallback_agent"] = self.fallback_agent
        if self.fallback_kind:
            data["fallback_kind"] = self.fallback_kind
        return data


class Agent(Protocol):
    def choose(self, view: dict, options: list[Action]) -> Decision:
        """Picks one of ``options`` given what the seat can see (``view``)."""
        ...
