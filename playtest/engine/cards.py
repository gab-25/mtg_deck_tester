"""Cards as the engine sees them: the printed facts, before any game starts."""

from dataclasses import dataclass, field

from .cardrules import CardFacts
from .manacost import ManaCost


class Kind:
    """How a card is played: as a land, or cast as one of these."""

    LAND = "land"
    CREATURE = "creature"
    # Artifacts, enchantments, planeswalkers, battles: they stay on the
    # battlefield but do nothing yet.
    PERMANENT = "permanent"
    # Instants, sorceries: they go to the graveyard without an effect yet.
    SPELL = "spell"


_PERMANENT_TYPES = ("Artifact", "Enchantment", "Planeswalker", "Battle")


@dataclass(frozen=True, slots=True)
class CardSpec:
    """The printed facts the engine needs about a card."""

    name: str
    kind: str
    mana_value: int = 0
    power: int = 0
    toughness: int = 0
    # None: the cost can't be read, and the card costs its mana value in generic mana.
    cost: ManaCost | None = None
    colors: tuple[str, ...] = ()
    # Colors (``"WUBRGC"``) its mana ability can make, and how much one activation makes.
    produced_mana: tuple[str, ...] = ()
    mana_amount: int = 0
    keywords: tuple[str, ...] = ()
    enters_tapped: bool = False
    legendary: bool = False
    types: tuple[str, ...] = ()
    facts: CardFacts | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_facts(cls, facts: CardFacts) -> CardSpec:
        if "Land" in facts.types:
            kind = Kind.LAND
        elif "Creature" in facts.types:
            kind = Kind.CREATURE
        elif any(t in facts.types for t in _PERMANENT_TYPES):
            kind = Kind.PERMANENT
        else:
            kind = Kind.SPELL
        return cls(
            name=facts.name,
            kind=kind,
            mana_value=facts.mana_value,
            power=facts.power or 0,
            toughness=facts.toughness or 0,
            cost=facts.mana_cost,
            colors=facts.colors,
            produced_mana=facts.produced_mana,
            mana_amount=facts.mana_amount,
            keywords=facts.keywords,
            enters_tapped=facts.enters_tapped,
            legendary=facts.legendary,
            types=facts.types,
            facts=facts,
        )

    @property
    def mana_cost(self) -> ManaCost:
        return self.cost if self.cost is not None else ManaCost(generic=self.mana_value)

    @property
    def mana_output(self) -> int:
        """Mana one activation makes. A land that makes mana makes at least one."""
        if not self.produced_mana:
            return 0
        return self.mana_amount or 1

    @property
    def instant_speed(self) -> bool:
        return "Instant" in self.types or "flash" in self.keywords

    def has(self, keyword: str) -> bool:
        return keyword in self.keywords


def cost_text(cost: ManaCost) -> str:
    """``{X}{2}{G}{G}``, as printed."""
    parts = ["{X}"] * cost.x
    if cost.generic or not cost.pips and not cost.x:
        parts.append(f"{{{cost.generic}}}")
    for symbol, count in cost.pips:
        parts += [f"{{{symbol}}}"] * count
    return "".join(parts)


@dataclass(frozen=True)
class DeckSpec:
    """A deck ready to be played: its commander and the other 99 cards."""

    name: str
    commander: CardSpec
    library: tuple[CardSpec, ...]
