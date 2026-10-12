"""Reading and checking the new-match form."""

import random
from dataclasses import dataclass

from decks.formats import DEFAULT_FORMAT, FORMATS
from decks.models import Deck, MetagameDeck

from . import benchmarks
from .agents import openrouter
from .models import Seat

# A seat's deck picker value for a bundled benchmark deck.
BENCHMARK_PREFIX = "benchmark:"
# A seat's deck picker value for a metagame deck.
METAGAME_PREFIX = "metagame:"
# Seats that ask a model through OpenRouter.
MODEL_AGENTS = {Seat.Agent.LLM, Seat.Agent.JEV}

# Rows the form shows; the format decides how many of them may be filled.
SEAT_ROWS = max(fmt.max_seats for fmt in FORMATS.values())
MAX_ROUNDS_LIMIT = 100


@dataclass
class SeatChoice:
    # A user's deck, or None for a bundled benchmark or a metagame deck.
    deck: Deck | None
    benchmark: str
    agent: str
    model: str
    metagame_deck: MetagameDeck | None = None

    @property
    def name(self) -> str:
        if self.deck is not None:
            return self.deck.name
        if self.metagame_deck is not None:
            return self.metagame_deck.archetype
        return benchmarks.label(self.benchmark)


@dataclass
class MatchForm:
    format: str
    seed: int
    max_rounds: int
    seats: list[SeatChoice]


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_match_form(post, user) -> tuple[MatchForm | None, list[str]]:
    """Returns the chosen match set-up, or ``None`` and every reason it is refused."""
    errors = []
    fmt = post.get("format") if post.get("format") in FORMATS else DEFAULT_FORMAT
    rules = FORMATS[fmt]

    raw_seed = (post.get("seed") or "").strip()
    seed = _int(raw_seed) if raw_seed else random.randrange(2**31)
    if seed is None:
        errors.append("The seed must be a whole number.")

    max_rounds = _int(post.get("max_rounds"), 20)
    if not 1 <= max_rounds <= MAX_ROUNDS_LIMIT:
        errors.append(f"The round limit must be between 1 and {MAX_ROUNDS_LIMIT}.")

    decks = {str(d.id): d for d in Deck.objects.filter(owner=user, status=Deck.Status.READY)}
    agents = {choice for choice, _ in Seat.Agent.choices}
    seats = []
    for row in range(SEAT_ROWS):
        deck_id = post.get(f"seat-{row}-deck") or ""
        if not deck_id:
            continue
        agent = post.get(f"seat-{row}-agent") or Seat.Agent.HEURISTIC
        if agent not in agents:
            errors.append(f"Seat {row + 1}: unknown agent {agent!r}.")
        model = (post.get(f"seat-{row}-model") or "").strip()
        if deck_id.startswith(METAGAME_PREFIX):
            meta = MetagameDeck.objects.filter(pk=_int(deck_id.removeprefix(METAGAME_PREFIX))).first()
            if meta is None:
                errors.append(f"Seat {row + 1}: that metagame deck is gone; sync again or pick another.")
            elif meta.format != fmt:
                errors.append(f"Seat {row + 1}: {meta.archetype} is not a {rules.label} metagame deck.")
            elif meta.status != MetagameDeck.Status.READY:
                errors.append(f"Seat {row + 1}: {meta.archetype} could not be imported ({meta.error}).")
            else:
                seats.append(SeatChoice(deck=None, benchmark="", agent=agent, model=model, metagame_deck=meta))
            continue
        if deck_id.startswith(BENCHMARK_PREFIX):
            key = deck_id.removeprefix(BENCHMARK_PREFIX)
            if key not in benchmarks.BENCHMARKS:
                errors.append(f"Seat {row + 1}: unknown benchmark deck {key!r}.")
                continue
            if benchmarks.family(key) != rules.family:
                errors.append(f"Seat {row + 1}: {benchmarks.label(key)} is not a {rules.label} benchmark.")
                continue
            seats.append(SeatChoice(deck=None, benchmark=key, agent=agent, model=model))
            continue
        deck = decks.get(deck_id)
        if deck is None:
            errors.append(f"Seat {row + 1}: pick one of your imported decks.")
            continue
        if deck.format != fmt:
            errors.append(f"Seat {row + 1}: {deck.name} is not a {rules.label} deck.")
        seats.append(SeatChoice(deck=deck, benchmark="", agent=agent, model=model))

    if not rules.min_seats <= len(seats) <= rules.max_seats:
        if rules.min_seats == rules.max_seats:
            wanted = f"exactly {rules.min_seats}"
        else:
            wanted = f"{rules.min_seats} to {rules.max_seats}"
        errors.append(f"{rules.label} is played by {wanted} players; {len(seats)} chosen.")

    if any(s.agent in MODEL_AGENTS for s in seats) and not openrouter.api_key():
        errors.append("Jev and LLM seats need an OpenRouter API key: set OPENROUTER_API_KEY.")

    if errors:
        return None, errors
    return MatchForm(format=fmt, seed=seed, max_rounds=max_rounds, seats=seats), []
