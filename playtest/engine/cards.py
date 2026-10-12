"""Cards as the kernel plays them."""

from dataclasses import dataclass

from .cardrules import SUPPORTED_KEYWORDS, CardFacts
from .dsl import CardProgram, Trigger
from .manacost import ManaCost, ManaCostError, parse_mana_cost


def _cost(facts: CardFacts) -> ManaCost:
    """The card's cost; one the kernel cannot read becomes generic mana of its mana value."""
    try:
        return parse_mana_cost(facts.mana_cost)
    except ManaCostError:
        return ManaCost(generic=facts.mana_value)


@dataclass(frozen=True, slots=True)
class CardSpec:
    """The printed facts of a card plus its compiled program.

    ``program`` is None for a card the judge could not compile: a permanent
    then plays as a vanilla object with its printed body and keywords, and an
    instant or sorcery cannot be cast at all.
    """

    name: str
    types: tuple[str, ...]
    cost: ManaCost = ManaCost()
    subtypes: tuple[str, ...] = ()
    supertypes: tuple[str, ...] = ()
    power: int | None = None
    toughness: int | None = None
    colors: tuple[str, ...] = ()
    produced_mana: tuple[str, ...] = ()
    mana_amount: int = 0
    enters_tapped: bool = False
    keywords: tuple[str, ...] = ()
    ward: int = 0
    program: CardProgram | None = CardProgram()

    @classmethod
    def from_facts(cls, facts: CardFacts, program: CardProgram | None) -> "CardSpec":
        return cls(
            name=facts.name,
            types=facts.types,
            cost=_cost(facts),
            subtypes=facts.subtypes,
            supertypes=facts.supertypes,
            power=facts.power,
            toughness=facts.toughness,
            colors=facts.colors,
            produced_mana=facts.produced_mana,
            mana_amount=facts.mana_amount,
            enters_tapped=facts.enters_tapped,
            keywords=facts.keywords,
            ward=facts.ward,
            program=program,
        )

    @property
    def supported(self) -> bool:
        return self.program is not None

    @property
    def mana_value(self) -> int:
        return self.cost.mana_value

    @property
    def is_land(self) -> bool:
        return "land" in self.types

    @property
    def is_creature(self) -> bool:
        return "creature" in self.types

    @property
    def is_instant(self) -> bool:
        return "instant" in self.types

    @property
    def is_spell_only(self) -> bool:
        """An instant or sorcery: it never becomes a permanent."""
        return "instant" in self.types or "sorcery" in self.types

    @property
    def is_permanent(self) -> bool:
        return not self.is_spell_only

    @property
    def legendary(self) -> bool:
        return "legendary" in self.supertypes

    @property
    def is_mana_source(self) -> bool:
        return self.mana_amount > 0 and bool(self.produced_mana)

    def has(self, keyword: str) -> bool:
        return keyword in self.keywords

    def abilities(self, trigger: str) -> list:
        return self.program.by_trigger(trigger) if self.program is not None else []

    @property
    def spell_abilities(self) -> list:
        return self.abilities(Trigger.SPELL)

    @property
    def type_label(self) -> str:
        return " ".join(t.capitalize() for t in self.types)


def card(
    name: str,
    type_line: str = "Creature",
    cost: str = "",
    power: int | None = None,
    toughness: int | None = None,
    *,
    keywords: tuple[str, ...] = (),
    produces: str = "",
    amount: int = 1,
    enters_tapped: bool = False,
    program: CardProgram | None = CardProgram(),
    colors: tuple[str, ...] | None = None,
    ward: int = 0,
) -> CardSpec:
    """A card built by hand (tests, scripted games): ``card("Bear", "Creature", "{1}{G}", 2, 2)``."""
    words = type_line.lower().split()
    supertypes = tuple(w for w in words if w in ("legendary", "basic", "snow"))
    types = tuple(w for w in words if w not in supertypes)
    unknown = [k for k in keywords if k not in SUPPORTED_KEYWORDS]
    if unknown:
        raise ValueError(f"unsupported keywords {unknown}")
    parsed = parse_mana_cost(cost)
    if colors is None:
        colors = tuple(c for c in "WUBRG" if any(c in pip for pip in parsed.pips))
    is_creature = "creature" in types
    return CardSpec(
        name=name,
        types=types,
        cost=parsed,
        supertypes=supertypes,
        power=(power or 0) if is_creature else None,
        toughness=(toughness or 0) if is_creature else None,
        colors=colors,
        produced_mana=tuple(c for c in "WUBRGC" if c in produces),
        mana_amount=amount if produces else 0,
        enters_tapped=enters_tapped,
        keywords=tuple(keywords),
        ward=ward,
        program=program,
    )


@dataclass(frozen=True, slots=True)
class DeckSpec:
    """A deck ready to be played: its commander and the other 99 cards."""

    name: str
    commander: CardSpec
    library: tuple[CardSpec, ...]
