"""The deterministic pre-pass: everything about a card that needs no model.

Mana cost, types, power/toughness, colors, printed keywords, "enters tapped"
and — critically — the mana a card makes come straight from Scryfall JSON.
What is left of the rules text afterwards, the *residual*, is all the judge
ever sees: an empty residual is a fully supported card with no model call, and
a residual matching the blocklist is unsupported with no model call either.

Pure: no Django, no I/O.
"""

import re
from dataclasses import dataclass

from .manacost import MANA_SYMBOLS, ManaCostError, parse_mana_cost


class ResidualClass:
    EMPTY = "empty"
    BLOCKED = "blocked"
    NEEDS_JUDGE = "needs_judge"


# Keywords that stand for a triggered ability the DSL states (CR 702.108 prowess).
KEYWORD_RULES = {
    "prowess": "whenever you cast a noncreature spell, {noun} gets +1/+1 until end of turn.",
}

# The printed keywords the kernel implements (lowercase).
SUPPORTED_KEYWORDS = (
    "flying",
    "trample",
    "haste",
    "vigilance",
    "lifelink",
    "deathtouch",
    "first strike",
    "double strike",
    "menace",
    "reach",
    "defender",
    "flash",
    "hexproof",
    "indestructible",
    "ward",
)

# Mechanics the DSL cannot express, refused without asking the judge. Each
# entry is (regex over the lowercased residual, human-readable reason).
BLOCKLIST = (
    (r"\bcascade\b", "cascade"),
    (r"\bstorm\b", "storm"),
    (r"\bcop(?:y|ies)\b", "copies"),
    (r"\binstead\b", "replacement effect (instead)"),
    (r"\bsearch(?:es)? (?:their|his or her|target player's|an opponent's|that player's) library\b", "library search"),
    (r"\bfor each\b", "for each"),
    (r"\bas long as\b", "as long as"),
    (r"\bprotection from\b", "protection"),
    (r"\bcan't\b|\bcannot\b", "restriction (can't)"),
    (r"\bexile\b.*\buntil\b", "exile until"),
    (r"\bloyalty\b", "loyalty"),
    (r"\bmutate\b", "mutate"),
    (r"\bdaybound\b|\bnightbound\b", "day/night"),
    (r"\brole token\b", "role token"),
    (r"\bdungeon\b|\bventure into\b|\binitiative\b", "dungeon"),
    (r"\bx is\b", "X is"),
    (r"^enchant\b", "aura"),
    (r"\bequip\b|\battach", "equipment"),
    (r"\btransform\b|\bconvert\b", "transform"),
    (r"\bemblem\b|\bthe monarch\b", "emblem / monarch"),
    (r"\bthis spell costs\b|\bcosts? \{\d+\} less\b|\bcosts? \{\d+\} more\b", "cost modification"),
    (r"\byou may cast\b|\bwithout paying\b", "free or alternative casting"),
)

# Supertypes the type line may carry; the rest of the words before the dash are types.
_SUPERTYPES = {"basic", "legendary", "snow", "world", "ongoing", "token", "elite", "host"}

# Card types whose rules the kernel cannot run at all.
BLOCKED_TYPES = {"planeswalker": "planeswalker", "saga": "saga", "dungeon": "dungeon"}

_REMINDER = re.compile(r"\s*\([^)]*\)")
# Regeneration does not exist in the kernel, so forbidding it changes nothing.
_NO_REGENERATION = re.compile(r"\s*(?:it|they) can't be regenerated\.", re.I)
_LEADING_INT = re.compile(r"^\d+")
_ENTERS_TAPPED = re.compile(r"\benters(?: the battlefield)? tapped\b")
# A free mana ability: "{T}: Add ..." with nothing but the tap as its cost and
# nothing else on the line (a painland's damage makes its line not free).
_MANA_ABILITY = re.compile(
    r"^\{T\}(?:, (?:pay (?P<life>\d+) life|(?P<sac>sacrifice) this (?:artifact|land|creature|permanent)))?"
    r": Add (?P<add>[^.]*)\."
    r"(?:\s*This (?:land|artifact|creature|permanent) deals (?P<damage>\d+) damage to you\.)?"
    r"(?:\s*Activate only if you control an? (?P<need1>\w+)(?: or an? (?P<need2>\w+))?\.)?$",
    re.I,
)
# What such an ability may add: fixed symbols, a choice of symbols, or N mana of
# any color. Anything else ("{G} for each creature you control") is not read here.
_ADD_CLAUSE = re.compile(
    r"^(?:(?:\{[WUBRGC]\})+(?:,? (?:or )?(?:\{[WUBRGC]\})+)*"
    r"|(?:one|two|three) mana of any (?:one )?(?:color|type)\b.*)$"
)
# Basic and typed lands carry their mana ability as reminder text only.
_REMINDER_MANA = re.compile(r"^\((\{T\}: Add [^)]*\.)\)$")
_WARD = re.compile(r"^ward \{(\d+)\}$")

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


@dataclass(frozen=True, slots=True)
class ManaAbility:
    """One way a permanent taps for mana, read from its text (never by a model)."""

    # The colors (and C) it can make; always a subset of Scryfall's produced_mana.
    colors: str
    amount: int = 1
    # Life paid (or damage dealt to its controller) per activation.
    life: int = 0
    sacrifice: bool = False
    # Land subtypes one of which its controller must control to activate it.
    needs: tuple[str, ...] = ()

    @property
    def free(self) -> bool:
        return not (self.life or self.sacrifice or self.needs)


@dataclass(frozen=True, slots=True)
class AltCost:
    """A cost a spell may be cast for instead of its mana cost, read from its text."""

    life: int = 0
    # Cards of this color (W, U, B, R, G) exiled from its caster's hand, and how many.
    exile_color: str = ""
    exile_count: int = 0
    # A land of this subtype its caster controls, returned to its owner's hand.
    return_land: str = ""
    # When it may be used: "" always, "NOT_YOUR_TURN", "CONTROL_COMMANDER".
    condition: str = ""


@dataclass(frozen=True, slots=True)
class CardFacts:
    """What the kernel knows about a card before any model is involved."""

    name: str
    oracle_id: str
    type_line: str
    types: tuple[str, ...]
    subtypes: tuple[str, ...]
    supertypes: tuple[str, ...]
    mana_cost: str
    mana_value: int
    power: int | None
    toughness: int | None
    colors: tuple[str, ...]
    # Colors (and C) this card's mana ability can make, from ``produced_mana``.
    produced_mana: tuple[str, ...]
    # Mana one activation makes (Sol Ring: 2); 0 when the card is no mana source.
    mana_amount: int
    enters_tapped: bool
    keywords: tuple[str, ...]
    ward: int
    # Rules text of the front face, as printed (reminder text included).
    oracle_text: str
    # The rules text left for the judge, one ability per line, lowercased,
    # with the card's own name replaced by "this <type>".
    residual: str
    residual_class: str
    block_reason: str = ""
    # Every blocking reason, not just the first (for measuring what to extend).
    block_reasons: tuple[str, ...] = ()
    # Every mana ability, free or with a cost; ``produced_mana`` / ``mana_amount``
    # summarize them.
    mana_abilities: tuple[ManaAbility, ...] = ()
    # Multi-faced cards are played as their front face only.
    partial: bool = False
    # How the card may be cast or used beyond its mana cost, read in code:
    # alternative costs, a permanent sacrificed as an additional cost
    # ("artifact or creature"…), "can't be countered", and the flashback,
    # cycling and landcycling costs (mana, "" when absent).
    alt_costs: tuple[AltCost, ...] = ()
    extra_sacrifice: str = ""
    uncounterable: bool = False
    flashback: str = ""
    cycling: str = ""
    # What landcycling finds: a search filter ("TYPES:island", "BASIC_LAND").
    landcycling: str = ""

    @property
    def legendary(self) -> bool:
        return "legendary" in self.supertypes

    def has_type(self, card_type: str) -> bool:
        return card_type in self.types

    @property
    def is_land(self) -> bool:
        return "land" in self.types

    @property
    def is_creature(self) -> bool:
        return "creature" in self.types

    @property
    def is_instant_or_sorcery(self) -> bool:
        return "instant" in self.types or "sorcery" in self.types

    @property
    def is_permanent(self) -> bool:
        return not self.is_instant_or_sorcery

    @property
    def is_mana_source(self) -> bool:
        return self.mana_amount > 0 and bool(self.produced_mana)


def printed_stat(value) -> int | None:
    """A printed power/toughness as a number: ``"3"`` -> 3, ``"1+*"`` -> 1, ``"*"`` -> 0."""
    if value is None:
        return None
    match = _LEADING_INT.match(str(value))
    return int(match.group(0)) if match else 0


def number_value(word: str) -> int | None:
    """``"3"`` / ``"three"`` -> 3; anything else -> None."""
    word = word.lower()
    if word.isdigit():
        return int(word)
    return NUMBER_WORDS.get(word)


def _front(card: dict) -> tuple[dict, bool]:
    faces = card.get("card_faces") or []
    if faces:
        return faces[0], True
    return card, False


def _split_type_line(type_line: str) -> tuple[tuple, tuple, tuple]:
    left, _, right = type_line.partition("—")
    words = left.lower().split()
    supertypes = tuple(w for w in words if w in _SUPERTYPES)
    types = tuple(w for w in words if w not in _SUPERTYPES)
    subtypes = tuple(right.lower().split())
    return supertypes, types, subtypes


def self_noun(types: tuple[str, ...]) -> str:
    """How rules text refers to the card itself: ``this creature``, ``this spell``…"""
    if "instant" in types or "sorcery" in types:
        return "this spell"
    for card_type in ("creature", "land", "artifact", "enchantment", "planeswalker", "battle"):
        if card_type in types:
            return f"this {card_type}"
    return "this permanent"


def _self_names(name: str) -> list[str]:
    names = [name]
    # Legendary cards refer to themselves by the short name: "Atraxa".
    short = name.split(",")[0].strip()
    if short != name and len(short) > 2:
        names.append(short)
    return names


def _mana_amount(add_clause: str) -> int:
    """How much mana an "Add ..." clause makes in one activation."""
    clause = add_clause.strip()
    words = re.match(r"^(\w+) mana\b", clause, re.I)
    if words and number_value(words.group(1)):
        return number_value(words.group(1))
    # "{G} or {U}", "{R}, {G}, or {W}": one symbol per option.
    options = re.split(r",?\s+or\s+|,\s+", clause)
    return max((len(re.findall(r"\{[^}]+\}", option)) for option in options), default=1) or 1


_COLOR_NAMES = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
_ALT_COST = re.compile(
    r"^(?:if (?P<cond>it's not your turn|you control a commander), )?you may "
    r"(?:(?P<parts>.+?) rather than pay this spell's mana cost|(?P<free>cast this spell without paying its mana cost))\.$"
)
_ALT_CONDITIONS = {"it's not your turn": "NOT_YOUR_TURN", "you control a commander": "CONTROL_COMMANDER"}
_EXTRA_SACRIFICE = re.compile(
    r"^as an additional cost to cast this spell, sacrifice an? "
    r"(?P<what>artifact or creature|creature|artifact|enchantment|land|nonland permanent|permanent)\.$"
)
_CYCLING = re.compile(r"^(?:(?P<type>plains|island|swamp|mountain|forest|basic land)?cycling) (?P<cost>(?:\{[^}]+\})+)$")
_FLASHBACK = re.compile(r"^flashback (?P<cost>(?:\{[^}]+\})+)$")


def _alt_cost(line: str) -> AltCost | None:
    """An alternative cost line read in code, or None when it says anything else."""
    match = _ALT_COST.match(line)
    if not match:
        return None
    fields: dict = {"condition": _ALT_CONDITIONS.get(match.group("cond") or "", "")}
    if match.group("free"):
        # Only a condition makes casting for free a choice the kernel can check.
        return AltCost(**fields) if fields["condition"] else None
    for part in match.group("parts").split(" and "):
        life = re.fullmatch(r"pay (\d+) life", part)
        exile = re.fullmatch(r"exile (a|one|two) (white|blue|black|red|green) cards? from your hand", part)
        bounce = re.fullmatch(r"return an? (plains|island|swamp|mountain|forest) you control to its owner's hand", part)
        if life:
            fields["life"] = int(life.group(1))
        elif exile:
            fields["exile_color"] = _COLOR_NAMES[exile.group(2)]
            fields["exile_count"] = 1 if exile.group(1) in ("a", "one") else 2
        elif bounce:
            fields["return_land"] = bounce.group(1)
        else:
            return None
    return AltCost(**fields)


def _keyword_line(line: str, card_keywords: set[str]) -> list[str] | None:
    """The keywords a line consists of, or None when it is not a keyword line."""
    parts = [p.strip() for p in re.split(r"[,;]", line) if p.strip()]
    found = []
    for part in parts:
        lowered = part.lower()
        match = next(
            (
                kw
                for kw in sorted(card_keywords, key=len, reverse=True)
                if lowered == kw
                or lowered.startswith(kw + " ")
                and re.fullmatch(r"(\{[^}]+\})+|\d+|—.*", lowered[len(kw) + 1 :].strip())
            ),
            None,
        )
        if match is None:
            return None
        found.append(lowered)
    return found


def characteristics(card: dict) -> CardFacts:
    """Derives a card's :class:`CardFacts` from its raw Scryfall JSON."""
    face, partial = _front(card)
    name = face.get("name") or card.get("name") or "Unknown card"
    type_line = (face.get("type_line") or card.get("type_line") or "").split("//")[0].strip()
    supertypes, types, subtypes = _split_type_line(type_line)

    raw_cost = face.get("mana_cost") if "mana_cost" in face else card.get("mana_cost", "")
    # Every reason the card cannot be compiled without the judge, in order.
    reasons: list[str] = []
    try:
        mana_value = parse_mana_cost(raw_cost or "").mana_value
    except ManaCostError as exc:
        mana_value = int(card.get("cmc") or 0)
        reasons.append(str(exc))

    if "power" in face:
        power, toughness = printed_stat(face.get("power")), printed_stat(face.get("toughness"))
    else:
        power, toughness = printed_stat(card.get("power")), printed_stat(card.get("toughness"))

    colors = face.get("colors", card.get("colors")) or []
    produced = [c for c in MANA_SYMBOLS if c in (card.get("produced_mana") or [])]
    oracle_text = face.get("oracle_text") or ""
    card_keywords = {k.lower() for k in card.get("keywords") or []}
    noun = self_noun(types)

    keywords: list[str] = []
    ward = 0
    enters_tapped = False
    mana_abilities: list[ManaAbility] = []
    residual_lines: list[str] = []
    casting: dict = {"alt_costs": []}
    for raw_line in oracle_text.splitlines():
        reminder = _REMINDER_MANA.match(raw_line.strip())
        line = reminder.group(1) if reminder else _NO_REGENERATION.sub("", _REMINDER.sub("", raw_line)).strip()
        if not line:
            continue
        for self_name in _self_names(name):
            line = line.replace(self_name, noun)

        mana = _MANA_ABILITY.match(line)
        if mana and produced and _ADD_CLAUSE.match(mana.group("add")) and "any one color" not in mana.group("add"):
            # The colors this ability names; "any color" takes Scryfall's list.
            named = set(re.findall(r"\{([WUBRGC])\}", mana.group("add")))
            # "Any color" never includes colorless.
            colors = "".join(c for c in produced if c in (named or set("WUBRG")))
            if colors:
                mana_abilities.append(
                    ManaAbility(
                        colors=colors,
                        amount=_mana_amount(mana.group("add")),
                        life=int(mana.group("life") or 0) + int(mana.group("damage") or 0),
                        sacrifice=bool(mana.group("sac")),
                        needs=tuple(n.lower() for n in (mana.group("need1"), mana.group("need2")) if n),
                    )
                )
                continue

        lowered = line.lower()
        if "instant" in types or "sorcery" in types or "creature" in types:
            alt = _alt_cost(lowered)
            if alt is not None:
                casting["alt_costs"].append(alt)
                continue
            extra = _EXTRA_SACRIFICE.match(lowered)
            if extra and "extra_sacrifice" not in casting:
                casting["extra_sacrifice"] = extra.group("what")
                continue
        if lowered == "this spell can't be countered.":
            casting["uncounterable"] = True
            continue
        if _ENTERS_TAPPED.search(lowered) and (
            lowered.startswith(noun) or lowered.startswith("as " + noun)
        ):
            enters_tapped = True
            continue

        found = _keyword_line(line, card_keywords)
        if found is not None:
            unsupported = []
            for keyword in found:
                ward_match = _WARD.match(keyword)
                cycling = _CYCLING.match(keyword)
                flashback = _FLASHBACK.match(keyword)
                if cycling:
                    kind = cycling.group("type")
                    if kind is None:
                        casting["cycling"] = cycling.group("cost").upper()
                    else:
                        casting["landcycling"] = "BASIC_LAND" if kind == "basic land" else f"TYPES:{kind}"
                        casting["landcycling_cost"] = cycling.group("cost").upper()
                    continue
                if flashback and ("instant" in types or "sorcery" in types):
                    casting["flashback"] = flashback.group("cost").upper()
                    continue
                if ward_match:
                    keywords.append("ward")
                    ward = int(ward_match.group(1))
                elif keyword in SUPPORTED_KEYWORDS:
                    keywords.append(keyword)
                elif keyword in KEYWORD_RULES:
                    # A keyword that is shorthand for a triggered ability: its
                    # rules text goes to the judge like any other line.
                    residual_lines.append(KEYWORD_RULES[keyword].format(noun=noun))
                else:
                    unsupported.append(keyword)
            if unsupported:
                reasons += [f"unsupported keyword: {keyword}" for keyword in unsupported]
                residual_lines.append(lowered)
            continue

        residual_lines.append(lowered)

    residual = "\n".join(residual_lines)
    blocked_type = next((BLOCKED_TYPES[t] for t in types + subtypes if t in BLOCKED_TYPES), "")
    if blocked_type:
        reasons.append(f"unsupported card type: {blocked_type}")
    reasons += [reason for pattern, reason in BLOCKLIST if re.search(pattern, residual, re.M)]
    block_reason = reasons[0] if reasons else ""

    if block_reason:
        residual_class = ResidualClass.BLOCKED
    elif residual:
        residual_class = ResidualClass.NEEDS_JUDGE
    else:
        residual_class = ResidualClass.EMPTY

    return CardFacts(
        name=name,
        oracle_id=card.get("oracle_id") or face.get("oracle_id") or card.get("id") or name,
        type_line=type_line,
        types=types,
        subtypes=subtypes,
        supertypes=supertypes,
        mana_cost=raw_cost or "",
        mana_value=mana_value,
        power=power if "creature" in types else None,
        toughness=toughness if "creature" in types else None,
        colors=tuple(c for c in "WUBRG" if c in colors),
        # Always a subset of Scryfall's produced_mana: never a model's guess.
        produced_mana=tuple(c for c in produced if any(c in a.colors for a in mana_abilities)),
        mana_amount=max((a.amount for a in mana_abilities), default=0),
        mana_abilities=tuple(mana_abilities),
        enters_tapped=enters_tapped,
        keywords=tuple(sorted(set(keywords), key=SUPPORTED_KEYWORDS.index)),
        ward=ward,
        oracle_text=oracle_text,
        residual=residual,
        residual_class=residual_class,
        block_reason=block_reason,
        block_reasons=tuple(reasons),
        partial=partial,
        alt_costs=tuple(casting["alt_costs"]),
        extra_sacrifice=casting.get("extra_sacrifice", ""),
        uncounterable=casting.get("uncounterable", False),
        flashback=casting.get("flashback", ""),
        cycling=casting.get("cycling", "") or casting.get("landcycling_cost", ""),
        landcycling=casting.get("landcycling", ""),
    )
