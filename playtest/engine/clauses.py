"""Splits a card's residual oracle text into abilities and atomic clauses.

Jev chooses, it doesn't generate structure, so the structure comes from here:
which trigger, what cost, which clauses in which order, and for every clause
the numbers and phrases it mentions. Anything this module can't read with
confidence raises :class:`Unreadable` and the card is UNSUPPORTED with no
model call.
"""

import re
from dataclasses import dataclass

from .cardrules import SUPPORTED_KEYWORDS, CardFacts
from .manacost import ManaCostError, parse_mana_cost

MAX_CLAUSES_PER_ABILITY = 4
MAX_ABILITIES = 12

_NUMBER_WORDS = {
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
_NUMBER = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_SUBJECT = r"(?:you|target player|target opponent|each player|each opponent)"
_VERBS = (
    r"(?:draws?|gains?|loses?|deals?|destroys?|exiles?|returns?|taps?|untaps?|adds?"
    r"|creates?|puts?|sacrifices?|mills?|counters?|discards?|gets?)\b"
)
_CLAUSE_BREAK = re.compile(rf",? then |,? and (?=(?:{_SUBJECT} )?{_VERBS})", re.IGNORECASE)
_LEADING_SUBJECT = re.compile(rf"^({_SUBJECT}) ", re.IGNORECASE)
_STARTS_WITH_VERB = re.compile(rf"^{_VERBS}", re.IGNORECASE)
_BACK_REFERENCE = re.compile(
    r"\b(?:it|its|it's|they|them|their|those|that (?:creature|player|permanent|card|spell|token))\b",
    re.IGNORECASE,
)
_OPTIONAL = re.compile(r"\bmay\b", re.IGNORECASE)
_RESTRICTION = re.compile(
    rf"\bwith (power|mana value) {_NUMBER} or (less|greater)\b", re.IGNORECASE
)
_TRAILING_QUALIFIER = re.compile(r"^ (?:with|without|that|which|who)\b", re.IGNORECASE)
_NOUN = r"(?:creature|player|opponent|artifact|enchantment|land|permanent|spell)s?"
_CONTROLLER = r"(?: you control| an opponent controls| your opponents control| you don't control)?"
_QUALIFIER_WORDS = {"creature", "artifact", "enchantment", "land", "nonland", "noncreature", "or"}
_PT_MOD = re.compile(r"([+-]\d+)/([+-]\d+)")
_TOKEN = re.compile(r"\b(\d+)/(\d+) ([A-Za-z ]+?) (?:artifact )?creature tokens?\b")
_COUNTER = re.compile(r"([+-]1/[+-]1) counters?\b")
_SACRIFICED = re.compile(
    rf"\bsacrifices? (?:a|an|{_NUMBER}) (creature|artifact|enchantment|land|permanent)s?\b",
    re.IGNORECASE,
)
_ARTICLE_ONE = re.compile(
    r"\b(?:draws?|creates?|puts?|sacrifices?|discards?|mills?) an? \b|\ba card\b", re.IGNORECASE
)
_MANA_RUN = re.compile(r"(?:\{[WUBRGC]\})+")
_SYMBOL = re.compile(r"\{[^}]+\}")
_COST_PART_MANA = re.compile(r"(?:\{[^}]+\})+")


class Unreadable(ValueError):
    """The text can't be split with confidence; the message says why."""


@dataclass(frozen=True, slots=True)
class Clause:
    text: str
    # Candidate amounts, in order of appearance.
    amounts: tuple[int, ...]
    # Whether it names a player or an object Jev must map to a selector.
    has_target: bool
    # A continuation ("... and loses 2 life") acting on the previous clause's
    # player, not on a new target.
    inherits_target: bool
    pt_mods: tuple[tuple[int, int], ...]
    token_pts: tuple[tuple[int, int], ...]
    token_names: tuple[str, ...]
    keywords: tuple[str, ...]
    counters: tuple[str, ...]
    colors: tuple[str, ...]
    card_types: tuple[str, ...]
    duration: str  # END_OF_TURN | PERMANENT
    restriction: dict | None


@dataclass(frozen=True, slots=True)
class AbilityText:
    trigger: str
    cost: dict | None
    mode_group: int | None
    clauses: tuple[Clause, ...]


def split_abilities(residual: str, facts: CardFacts) -> list[AbilityText]:
    """Splits ``residual`` into abilities; raises :class:`Unreadable`."""
    self_ref = rf"(?:this (?:creature|permanent|artifact|enchantment|land)|{re.escape(facts.name)})"
    is_spell = "Instant" in facts.types or "Sorcery" in facts.types
    abilities = []
    lines = residual.splitlines()
    mode_group = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Choose "):
            if line != "Choose one —":
                raise Unreadable(f"modal choice other than 'choose one': {line!r}")
            if not is_spell:
                raise Unreadable("modal ability on a permanent")
            mode_group += 1
            i += 1
            modes = 0
            while i < len(lines) and lines[i].startswith("• "):
                abilities.append(_ability(lines[i][2:], self_ref, is_spell, facts, mode_group))
                modes += 1
                i += 1
            if modes < 2:
                raise Unreadable("'choose one' with fewer than two modes")
            continue
        abilities.append(_ability(line, self_ref, is_spell, facts, None))
        i += 1
    if len(abilities) > MAX_ABILITIES:
        raise Unreadable(f"more than {MAX_ABILITIES} abilities")
    return abilities


def _ability(line, self_ref, is_spell, facts, mode_group) -> AbilityText:
    triggers = (
        ("ETB", rf"When(?:ever)? {self_ref} enters(?: the battlefield)?, "),
        ("DIES", rf"When(?:ever)? {self_ref} dies, "),
        ("ATTACK_TRIGGER", rf"Whenever {self_ref} attacks, "),
        ("UPKEEP_TRIGGER", r"At the beginning of your upkeep, "),
        ("END_STEP_TRIGGER", r"At the beginning of your end step, "),
    )
    cost = None
    for trigger, pattern in triggers:
        match = re.match(pattern, line)
        if match:
            effect = line[match.end() :]
            break
    else:
        if re.match(r"(?:When|Whenever|At) ", line):
            raise Unreadable(f"unsupported trigger: {line!r}")
        activated = re.match(r"([^:.]+): (.+)$", line)
        if activated:
            trigger = "ACTIVATED"
            cost = _cost(activated.group(1), self_ref)
            effect = activated.group(2)
        else:
            trigger = "SPELL" if is_spell else "STATIC"
            effect = line
    clauses = tuple(_clause(text, inherited, self_ref) for text, inherited in _clause_texts(effect))
    if not clauses:
        raise Unreadable(f"no effect in {line!r}")
    if len(clauses) > MAX_CLAUSES_PER_ABILITY:
        raise Unreadable(f"more than {MAX_CLAUSES_PER_ABILITY} clauses in {line!r}")
    return AbilityText(trigger, cost, mode_group, clauses)


def _cost(text, self_ref) -> dict:
    mana = ""
    tap = False
    sacrifice_self = False
    for part in text.split(", "):
        if part == "{T}":
            tap = True
        elif _COST_PART_MANA.fullmatch(part):
            try:
                parse_mana_cost(part)
            except ManaCostError as exc:
                raise Unreadable(f"unsupported cost {part!r}") from exc
            mana += part
        elif re.fullmatch(rf"Sacrifice {self_ref}", part):
            sacrifice_self = True
        else:
            raise Unreadable(f"unsupported cost {part!r}")
    return {"mana": mana, "tap": tap, "sacrifice_self": sacrifice_self}


def _clause_texts(effect: str) -> list[tuple[str, bool]]:
    """``(text, inherits_target)`` for every clause, in order."""
    texts = []
    for sentence in re.split(r"(?<=\.)\s+", effect.strip()):
        sentence = re.sub(r"^Then,? ", "", sentence.strip().rstrip("."))
        subject = None
        for piece in _CLAUSE_BREAK.split(sentence):
            piece = piece.strip()
            if not piece:
                continue
            leading = _LEADING_SUBJECT.match(piece)
            inherited = False
            if leading:
                subject = leading.group(1)
            elif subject and _STARTS_WITH_VERB.match(piece):
                # "Target player draws two cards and loses 2 life": the second
                # clause keeps the first one's subject — the same player.
                piece = f"{subject} {piece}"
                inherited = True
            texts.append((piece, inherited))
    return texts


def _clause(text: str, inherited: bool, self_ref: str) -> Clause:
    if _OPTIONAL.search(text):
        raise Unreadable(f"optional effect ('may'): {text!r}")
    if _BACK_REFERENCE.search(text):
        raise Unreadable(f"refers back to an earlier object: {text!r}")

    restriction = None
    match = _RESTRICTION.search(text)
    if match:
        stat, number, direction = match.groups()
        kind = ("POWER" if stat.lower() == "power" else "MV") + (
            "_LE" if direction.lower() == "less" else "_GE"
        )
        restriction = {"kind": kind, "value": _number(number)}
    # The card naming itself as the source ("Test Card deals 3 damage to ...")
    # is not what the clause affects.
    subject = re.match(rf"{self_ref} ", text)
    phrases = _target_phrases(text[subject.end() :] if subject else text, self_ref)
    if len(phrases) > 1:
        raise Unreadable(f"several players or objects in one clause: {text!r}")

    pt_mods = tuple(
        (int(p), int(t)) for p, t in _PT_MOD.findall(text) if not re.search(r"counter", text)
    )
    tokens = _TOKEN.findall(text)
    mana_runs = _MANA_RUN.findall(text) if re.search(r"\badds?\b", text, re.IGNORECASE) else []
    return Clause(
        text=text,
        amounts=_amounts(text, mana_runs),
        has_target=bool(phrases) and not inherited,
        inherits_target=inherited,
        pt_mods=pt_mods,
        token_pts=tuple((int(p), int(t)) for p, t, _ in tokens),
        token_names=tuple(name.strip() for _, _, name in tokens),
        keywords=tuple(
            k for k in SUPPORTED_KEYWORDS if re.search(rf"\b{k}\b", text, re.IGNORECASE)
        ),
        counters=tuple(dict.fromkeys(_COUNTER.findall(text))),
        colors=tuple(dict.fromkeys(s[1] for run in mana_runs for s in _SYMBOL.findall(run))),
        card_types=tuple(dict.fromkeys(m[-1].upper() for m in _SACRIFICED.findall(text))),
        duration="END_OF_TURN" if "until end of turn" in text.lower() else "PERMANENT",
        restriction=restriction,
    )


def _target_phrases(text: str, self_ref: str) -> list[str]:
    pattern = re.compile(
        r"\bany target\b"
        rf"|\btarget (?P<tq>[a-z ]*?){_NOUN}{_CONTROLLER}\b"
        rf"|\beach (?P<eq>[a-z ]*?)(?:opponent|player|creature)s?{_CONTROLLER}\b"
        rf"|\b(?:all )?creatures{_CONTROLLER}\b"
        rf"|\b{self_ref}\b"
        r"|\byou\b",
        re.IGNORECASE,
    )
    phrases = []
    for match in pattern.finditer(text):
        qualifier = match.group("tq") or match.group("eq") or ""
        if set(qualifier.split()) - _QUALIFIER_WORDS:
            raise Unreadable(f"unsupported qualifier {qualifier.strip()!r}: {text!r}")
        after = text[match.end() :]
        if _TRAILING_QUALIFIER.match(after) and not _RESTRICTION.match(after.lstrip()):
            raise Unreadable(f"unsupported qualifier after {match.group(0)!r}: {text!r}")
        phrases.append(match.group(0))
    return phrases


def _amounts(text: str, mana_runs: list[str]) -> tuple[int, ...]:
    found = [run.count("{") for run in mana_runs]
    if _ARTICLE_ONE.search(text):
        found.append(1)
    # Numbers that are part of a P/T, a counter, a mana symbol or a restriction
    # are not amounts.
    plain = _RESTRICTION.sub(" ", text)
    plain = re.sub(r"[+-]?\d+/[+-]?\d+", " ", plain)
    plain = _SYMBOL.sub(" ", plain)
    found += [_number(n) for n in re.findall(rf"\b{_NUMBER}\b", plain, re.IGNORECASE)]
    return tuple(dict.fromkeys(found))


def _number(text: str) -> int:
    return int(text) if text.isdigit() else _NUMBER_WORDS[text.lower()]
