"""Reading and checking the new-playtest form: a deck to test and its opponents."""

import random
from dataclasses import dataclass

from decks.formats import FORMATS, playable_formats
from decks.models import Deck, MetagameDeck

from .agents import openrouter
from .models import Seat

# An opponent picker value for a metagame deck.
METAGAME_PREFIX = "metagame:"
# Every seat of a new match is played by Jev, with the server's model
# (JEV_MODEL): the user picks decks, not agents or models. The other
# Seat.Agent values only name the seats of older matches; Forge's AI still
# takes fallbacks and the decisions the bridge does not expose.
SEAT_AGENT = Seat.Agent.JEV
# The most opponents one session plays: each match is a few hundred Jev calls.
MAX_OPPONENTS = 10
# The format the form starts on.
DEFAULT_MATCH_FORMAT = next(iter(playable_formats()))


@dataclass
class DeckChoice:
    # A user's deck, or None for a metagame deck.
    deck: Deck | None
    metagame_deck: MetagameDeck | None = None

    @property
    def name(self) -> str:
        if self.deck is not None:
            return self.deck.name
        return self.metagame_deck.archetype


@dataclass
class PlaytestForm:
    format: str
    seed: int
    deck: Deck
    opponents: list[DeckChoice]


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _opponent(value: str, decks: dict, fmt: str, errors: list) -> DeckChoice | None:
    label = FORMATS[fmt].label
    if value.startswith(METAGAME_PREFIX):
        meta = MetagameDeck.objects.filter(pk=_int(value.removeprefix(METAGAME_PREFIX))).first()
        if meta is None:
            errors.append("An opponent metagame deck is gone; sync again or pick another.")
        elif meta.format != fmt:
            errors.append(f"Opponent {meta.archetype} is not a {label} metagame deck.")
        elif meta.status != MetagameDeck.Status.READY:
            errors.append(f"Opponent {meta.archetype} could not be imported ({meta.error}).")
        else:
            return DeckChoice(deck=None, metagame_deck=meta)
        return None
    deck = decks.get(value)
    if deck is None:
        errors.append("Pick opponents among your imported decks and the metagame decks.")
        return None
    if deck.format != fmt:
        errors.append(f"Opponent {deck.name} is not a {label} deck.")
        return None
    return DeckChoice(deck=deck)


def parse_playtest_form(post, user) -> tuple[PlaytestForm | None, list[str]]:
    """Returns the chosen session, or ``None`` and every reason it is refused."""
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
    deck = decks.get(post.get("deck") or "")
    if deck is None:
        errors.append("Pick the deck to test among your imported decks.")
    elif deck.format != fmt:
        errors.append(f"{deck.name} is not a {rules.label} deck.")

    values = list(dict.fromkeys(v for v in post.getlist("opponents") if v))
    if not values:
        errors.append("Pick at least one opponent.")
    elif len(values) > MAX_OPPONENTS:
        errors.append(f"Pick at most {MAX_OPPONENTS} opponents; {len(values)} chosen.")
    opponents = [o for o in (_opponent(v, decks, fmt, errors) for v in values[:MAX_OPPONENTS]) if o]

    if not openrouter.api_key():
        errors.append("Jev plays every seat and needs an OpenRouter API key: set OPENROUTER_API_KEY.")

    if errors:
        return None, errors
    return PlaytestForm(format=fmt, seed=seed, deck=deck, opponents=opponents), []
