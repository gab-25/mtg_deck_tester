"""Reading and checking the new-match form."""

import random
from dataclasses import dataclass

from decks.formats import FORMATS, playable_formats
from decks.models import Deck, MetagameDeck

from .agents import openrouter
from .models import Seat

# A seat's deck picker value for a metagame deck.
METAGAME_PREFIX = "metagame:"
# Seats that ask a model through OpenRouter.
MODEL_AGENTS = {Seat.Agent.LLM, Seat.Agent.JEV}
# The agents a new match may seat, in the order the form lists them.
PLAYABLE_AGENTS = [Seat.Agent.FORGE_AI, Seat.Agent.JEV, Seat.Agent.RANDOM, Seat.Agent.LLM]

# Rows the form shows; the format decides how many of them may be filled.
SEAT_ROWS = max(fmt.max_seats for fmt in playable_formats().values())
# The format the form starts on.
DEFAULT_MATCH_FORMAT = next(iter(playable_formats()))


def agent_choices() -> list[tuple[str, str]]:
    return [(agent.value, agent.label) for agent in PLAYABLE_AGENTS]


@dataclass
class SeatChoice:
    # A user's deck, or None for a metagame deck.
    deck: Deck | None
    agent: str
    model: str
    metagame_deck: MetagameDeck | None = None

    @property
    def name(self) -> str:
        if self.deck is not None:
            return self.deck.name
        return self.metagame_deck.archetype


@dataclass
class MatchForm:
    format: str
    seed: int
    seats: list[SeatChoice]


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_match_form(post, user) -> tuple[MatchForm | None, list[str]]:
    """Returns the chosen match set-up, or ``None`` and every reason it is refused."""
    errors = []
    fmt = post.get("format") if post.get("format") in FORMATS else DEFAULT_MATCH_FORMAT
    rules = FORMATS[fmt]
    if fmt not in playable_formats():
        errors.append(
            f"{rules.label} cannot be played yet: the Forge engine seats two players "
            f"({', '.join(f.label for f in playable_formats().values())})."
        )

    raw_seed = (post.get("seed") or "").strip()
    seed = _int(raw_seed) if raw_seed else random.randrange(2**31)
    if seed is None:
        errors.append("The seed must be a whole number.")

    decks = {str(d.id): d for d in Deck.objects.filter(owner=user, status=Deck.Status.READY)}
    agents = set(PLAYABLE_AGENTS)
    seats = []
    for row in range(SEAT_ROWS):
        deck_id = post.get(f"seat-{row}-deck") or ""
        if not deck_id:
            continue
        agent = post.get(f"seat-{row}-agent") or Seat.Agent.FORGE_AI
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
                seats.append(SeatChoice(deck=None, agent=agent, model=model, metagame_deck=meta))
            continue
        deck = decks.get(deck_id)
        if deck is None:
            errors.append(f"Seat {row + 1}: pick one of your imported decks.")
            continue
        if deck.format != fmt:
            errors.append(f"Seat {row + 1}: {deck.name} is not a {rules.label} deck.")
        seats.append(SeatChoice(deck=deck, agent=agent, model=model))

    if fmt in playable_formats() and not rules.min_seats <= len(seats) <= rules.max_seats:
        if rules.min_seats == rules.max_seats:
            wanted = f"exactly {rules.min_seats}"
        else:
            wanted = f"{rules.min_seats} to {rules.max_seats}"
        errors.append(f"{rules.label} is played by {wanted} players; {len(seats)} chosen.")

    if any(s.agent in MODEL_AGENTS for s in seats) and not openrouter.api_key():
        errors.append("Jev and LLM seats need an OpenRouter API key: set OPENROUTER_API_KEY.")

    if errors:
        return None, errors
    return MatchForm(format=fmt, seed=seed, seats=seats), []
