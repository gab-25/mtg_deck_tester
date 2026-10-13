"""An agent that picks uniformly at random: the free baseline, no network."""

import random

from .base import Decision


class RandomAgent:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)

    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "") -> Decision:
        return Decision(self.rng.randrange(len(options)))
