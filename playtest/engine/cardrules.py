"""Deterministic pre-pass: a card's printed facts, straight from Scryfall JSON.

No model is involved here. Mana cost, types, power/toughness, colors and — above
all — the mana a card produces come from Scryfall's data, so the mana base is
never guessed. What is left of the rules text once keywords, reminder text,
"enters tapped" and plain mana abilities are read off is the *residual* text:
the only part a judge ever sees.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from .manacost import ManaCost, ManaCostError, parse_mana_cost

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

# Mechanics the DSL can't express: a card whose residual text matches one is
# UNSUPPORTED without asking any model.
BLOCKLIST = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("cascade", r"\bcascade\b"),
        ("storm", r"\bstorm\b"),
        ("copy", r"\bcop(?:y|ies)\b"),
        ("instead", r"\binstead\b"),
        ("search your library", r"\bsearch(?:es)? (?:your|their|its owner's) library\b"),
        ("for each", r"\bfor each\b"),
        ("as long as", r"\bas long as\b"),
        ("protection from", r"\bprotection from\b"),
        ("can't", r"\bcan't\b"),
        ("exile ... until", r"\bexile\b[^.]*\buntil\b"),
        ("loyalty", r"\bloyalty\b"),
        ("mutate", r"\bmutate\b"),
        ("daybound", r"\b(?:daybound|nightbound)\b"),
        ("role token", r"\brole token\b"),
        ("dungeon", r"\b(?:dungeon|venture)\b"),
        ("X is", r"\bX is\b"),
    )
)

_SUPERTYPES = {"Basic", "Legendary", "Snow", "World", "Ongoing"}
_COLOR_ORDER = "WUBRGC"
_LEADING_INT = re.compile(r"^\d+")
_REMINDER = re.compile(r"\s*\([^()]*\)")
_WARD = re.compile(r"ward ((?:\{[^}]+\})+)", re.IGNORECASE)
_MANA_SYMBOLS = r"(?:\{[WUBRGC]\})"
_COUNT_WORDS = {"one": 1, "two": 2, "three": 3}
# "{T}: Add {G}." / "{T}: Add {C}{C}." / "{T}: Add {W} or {U}." / "{T}: Add one
# mana of any color." — Scryfall's produced_mana already says which colors.
_MANA_ABILITY = re.compile(
    rf"\{{T\}}: Add (?:(?P<run>{_MANA_SYMBOLS}+)"
    rf"|{_MANA_SYMBOLS}(?:, {_MANA_SYMBOLS})*,? or {_MANA_SYMBOLS}"
    r"|(?P<n>one|two|three) mana of any (?:one )?color)\."
)


class Residue(StrEnum):
    EMPTY = "empty"  # nothing left to compile: fully described by the facts
    BLOCKED = "blocked"  # matches the blocklist: unsupported, no model call
    NEEDS_JUDGE = "needs_judge"


@dataclass(frozen=True, slots=True)
class CardFacts:
    name: str  # front face name
    oracle_id: str
    type_line: str  # front face
    mana_cost: ManaCost | None  # None when the kernel can't read it
    mana_value: int
    supertypes: tuple[str, ...]
    types: tuple[str, ...]
    subtypes: tuple[str, ...]
    power: int | None
    toughness: int | None
    colors: tuple[str, ...]
    produced_mana: tuple[str, ...]
    # Mana one activation of its printed mana ability makes (0 when it has none).
    mana_amount: int
    enters_tapped: bool
    keywords: tuple[str, ...]
    ward_cost: ManaCost | None
    legendary: bool
    # A multi-face card, read from its front face only.
    partial: bool
    oracle_text: str  # front face, as printed
    residual: str
    residue: Residue
    blocked_by: str  # why the residue is BLOCKED, empty otherwise


def printed_stat(value) -> int:
    """A printed power/toughness as a number: ``"3"`` -> 3, ``"1+*"`` -> 1, ``"*"`` -> 0."""
    match = _LEADING_INT.match(str(value or ""))
    return int(match.group(0)) if match else 0


def characteristics(card: dict) -> CardFacts:
    """Reads a card's facts and residual text off its raw Scryfall JSON."""
    faces = card.get("card_faces") or []
    front = faces[0] if faces else card
    name = front.get("name") or card.get("name") or ""
    type_line = (front.get("type_line") or card.get("type_line") or "").split("//")[0].strip()
    supertypes, types, subtypes = _split_type_line(type_line)

    raw_cost = front.get("mana_cost", card.get("mana_cost")) or ""
    try:
        mana_cost = parse_mana_cost(raw_cost)
    except ManaCostError:
        mana_cost = None

    oracle_text = front.get("oracle_text", card.get("oracle_text")) or ""
    produced = tuple(c for c in _COLOR_ORDER if c in (card.get("produced_mana") or []))
    residual, keywords, ward_cost, enters_tapped, mana_amount = _read_text(
        oracle_text, name, bool(produced)
    )
    if produced and not mana_amount:
        # Basic lands print their mana ability as reminder text only.
        mana_amount = 1

    power = front.get("power", card.get("power"))
    toughness = front.get("toughness", card.get("toughness"))
    colors = front.get("colors", card.get("colors")) or []
    residue, blocked_by = _classify(residual, raw_cost, mana_cost, types, subtypes)

    return CardFacts(
        name=name,
        oracle_id=card.get("oracle_id") or "",
        type_line=type_line,
        mana_cost=mana_cost,
        mana_value=int(card.get("cmc") or 0),
        supertypes=supertypes,
        types=types,
        subtypes=subtypes,
        power=None if power is None else printed_stat(power),
        toughness=None if toughness is None else printed_stat(toughness),
        colors=tuple(c for c in _COLOR_ORDER if c in colors),
        produced_mana=produced,
        mana_amount=mana_amount,
        enters_tapped=enters_tapped,
        keywords=keywords,
        ward_cost=ward_cost,
        legendary="Legendary" in supertypes,
        partial=len(faces) > 1,
        oracle_text=oracle_text,
        residual=residual,
        residue=residue,
        blocked_by=blocked_by,
    )


def _split_type_line(type_line: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    left, _, right = type_line.partition("—")
    words = left.split()
    supertypes = tuple(w for w in words if w in _SUPERTYPES)
    types = tuple(w for w in words if w not in _SUPERTYPES)
    return supertypes, types, tuple(right.split())


def _read_text(oracle_text: str, name: str, produces_mana: bool):
    kept = []
    keywords: list[str] = []
    ward_cost = None
    enters_tapped = False
    mana_amount = 0
    enters_tapped_line = re.compile(
        rf"(?:this \w+|{re.escape(name)}) enters(?: the battlefield)? tapped\.", re.IGNORECASE
    )
    for line in oracle_text.splitlines():
        line = _REMINDER.sub("", line).strip()
        if not line:
            continue
        line_keywords = _keyword_line(line)
        if line_keywords is not None:
            for keyword, cost in line_keywords:
                keywords.append(keyword)
                ward_cost = cost or ward_cost
            continue
        if enters_tapped_line.fullmatch(line):
            enters_tapped = True
            continue
        mana = _MANA_ABILITY.fullmatch(line) if produces_mana else None
        if mana:
            if mana.group("run"):
                amount = mana.group("run").count("{")
            elif mana.group("n"):
                amount = _COUNT_WORDS[mana.group("n")]
            else:
                amount = 1
            mana_amount = max(mana_amount, amount)
            continue
        kept.append(line)
    ordered = tuple(k for k in SUPPORTED_KEYWORDS if k in keywords)
    return "\n".join(kept), ordered, ward_cost, enters_tapped, mana_amount


def _keyword_line(line: str):
    """``[(keyword, ward cost or None)]`` when the line is only supported keywords."""
    found = []
    for part in line.split(","):
        part = part.strip().lower()
        if part in SUPPORTED_KEYWORDS and part != "ward":
            found.append((part, None))
            continue
        ward = _WARD.fullmatch(part)
        if ward is None:
            return None
        try:
            found.append(("ward", parse_mana_cost(ward.group(1))))
        except ManaCostError:
            return None
    return found


def _classify(residual, raw_cost, mana_cost, types, subtypes) -> tuple[Residue, str]:
    if mana_cost is None:
        return Residue.BLOCKED, f"mana cost {raw_cost}"
    if "Planeswalker" in types:
        return Residue.BLOCKED, "planeswalker"
    if "Saga" in subtypes:
        return Residue.BLOCKED, "saga"
    if not residual:
        return Residue.EMPTY, ""
    for label, pattern in BLOCKLIST:
        if pattern.search(residual):
            return Residue.BLOCKED, label
    return Residue.NEEDS_JUDGE, ""
