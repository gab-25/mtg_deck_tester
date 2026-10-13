"""Reading and checking the new-match form."""

import random
from dataclasses import dataclass

from decks.formats import FORMATS, playable_formats
from decks.models import Deck, MetagameDeck

from .agents import openrouter
from .models import Seat

# A seat's deck picker value for a metagame deck.
METAGAME_PREFIX = "metagame:"
# Every seat of a new match is played by Jev, with the server's model
# (JEV_MODEL): the user picks decks, not agents or models.
# The other Seat.Agent values only name the seats of older matches; Forge's AI
# still takes fallbacks and the decisions the bridge does not expose.
SEAT_AGENT = Seat.Agent.JEV

# Rows the form shows; the format decides how many of them may be filled.
SEAT_ROWS = max(fmt.max_seats for fmt in playable_formats().values())
# The format the form starts on.
DEFAULT_MATCH_FORMAT = next(iter(playable_formats()))


@dataclass
class SeatChoice:
    # A user's deck, or None for a metagame deck.
    deck: Deck | None
    agent: str
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
    seats = []
    for row in range(SEAT_ROWS):
        deck_id = post.get(f"seat-{row}-deck") or ""
        if not deck_id:
            continue
        agent = SEAT_AGENT
        if deck_id.startswith(METAGAME_PREFIX):
            meta = MetagameDeck.objects.filter(pk=_int(deck_id.removeprefix(METAGAME_PREFIX))).first()
            if meta is None:
                errors.append(f"Seat {row + 1}: that metagame deck is gone; sync again or pick another.")
            elif meta.format != fmt:
                errors.append(f"Seat {row + 1}: {meta.archetype} is not a {rules.label} metagame deck.")
            elif meta.status != MetagameDeck.Status.READY:
                errors.append(f"Seat {row + 1}: {meta.archetype} could not be imported ({meta.error}).")
            else:
                seats.append(SeatChoice(deck=None, agent=agent, metagame_deck=meta))
            continue
        deck = decks.get(deck_id)
        if deck is None:
            errors.append(f"Seat {row + 1}: pick one of your imported decks.")
            continue
        if deck.format != fmt:
            errors.append(f"Seat {row + 1}: {deck.name} is not a {rules.label} deck.")
        seats.append(SeatChoice(deck=deck, agent=agent))

    if fmt in playable_formats() and not rules.min_seats <= len(seats) <= rules.max_seats:
        if rules.min_seats == rules.max_seats:
            wanted = f"exactly {rules.min_seats}"
        else:
            wanted = f"{rules.min_seats} to {rules.max_seats}"
        errors.append(f"{rules.label} is played by {wanted} players; {len(seats)} chosen.")

    if seats and not openrouter.api_key():
        errors.append("Jev plays every seat and needs an OpenRouter API key: set OPENROUTER_API_KEY.")

    if errors:
        return None, errors
    return MatchForm(format=fmt, seed=seed, seats=seats), []
