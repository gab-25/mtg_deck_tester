"""The formats a deck can be built for and a match can be played in."""

from dataclasses import dataclass


COMMANDER_FAMILY = "commander"
CONSTRUCTED_FAMILY = "constructed"


@dataclass(frozen=True)
class Format:
    """Everything that varies between the supported formats.

    Formats come in two families. Commander formats (100 cards, singleton, one
    commander, color identity) differ only in ban list and table; constructed
    formats (a 60-card main deck and a 15-card sideboard, four copies) differ
    only in ban list. The construction rules of each family live in
    :mod:`decks.rules.commander` and :mod:`decks.rules.constructed`.
    """

    label: str
    # Key read inside a card's Scryfall ``legalities`` dict.
    legality_key: str
    starting_life: int
    # How many players sit at a table of this format.
    min_seats: int
    max_seats: int
    # Combat damage from a single commander that eliminates a player, or None
    # when the format does not track commander damage.
    commander_damage_limit: int | None
    family: str = COMMANDER_FAMILY
    # Main-deck size: exactly ``deck_size`` when ``exact_size``, else at least.
    deck_size: int = 100
    exact_size: bool = True
    sideboard_size: int = 0
    max_copies: int = 1

    @property
    def has_commander(self) -> bool:
        return self.family == COMMANDER_FAMILY


def _constructed(label: str, key: str) -> Format:
    return Format(
        label=label,
        legality_key=key,
        starting_life=20,
        min_seats=2,
        max_seats=2,
        commander_damage_limit=None,
        family=CONSTRUCTED_FAMILY,
        deck_size=60,
        exact_size=False,
        sideboard_size=15,
        max_copies=4,
    )


CONSTRUCTED_FORMATS = ("standard", "pioneer", "modern", "legacy", "vintage", "pauper")

FORMATS: dict[str, Format] = {
    "commander": Format(
        label="Commander",
        legality_key="commander",
        starting_life=40,
        min_seats=2,
        max_seats=4,
        commander_damage_limit=21,
    ),
    "duel": Format(
        label="Duel Commander",
        legality_key="duel",
        starting_life=20,
        min_seats=2,
        max_seats=2,
        commander_damage_limit=None,
    ),
    **{key: _constructed(key.capitalize(), key) for key in CONSTRUCTED_FORMATS},
}

DEFAULT_FORMAT = "commander"


def format_choices() -> list[tuple[str, str]]:
    """The ``FORMATS`` registry as Django model/form choices."""
    return [(key, fmt.label) for key, fmt in FORMATS.items()]
