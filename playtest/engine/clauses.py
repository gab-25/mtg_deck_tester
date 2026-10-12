"""The clause splitter: the structure of a card's rules text, read in code.

The judge model does not generate structure, so the structure comes from here:
one ability per oracle line, the trigger read by regex, activated costs parsed,
modes split into abilities sharing a ``mode_group``, and every effect cut into
atomic clauses at deterministic boundaries, in their textual order (which is
the op order of the program). For every clause, the candidate amounts and
candidate selector phrases found in it are listed.

A line it cannot read is refused with a reason, before any model is asked.

Pure: no Django, no I/O.
"""

import re
from dataclasses import dataclass

from .cardrules import CardFacts, number_value
from .dsl import Cost, Trigger, selector_phrase_regex
from .manacost import ManaCostError, parse_mana_cost

MAX_CLAUSES_PER_ABILITY = 4
MAX_ABILITIES = 8

_TRIGGERS = (
    (re.compile(r"^when(?:ever)? this \w+ enters(?: the battlefield)?, (.+)$"), Trigger.ETB),
    (re.compile(r"^when(?:ever)? this \w+ dies, (.+)$"), Trigger.DIES),
    (re.compile(r"^whenever this \w+ attacks, (.+)$"), Trigger.ATTACKS),
    (re.compile(r"^at the beginning of your upkeep, (.+)$"), Trigger.UPKEEP),
    (re.compile(r"^at the beginning of your end step, (.+)$"), Trigger.END_STEP),
)
_OTHER_TRIGGER = re.compile(r"^(?:when|whenever|at the beginning|at end)\b")
_ACTIVATED = re.compile(r"^([^:\"]+): (.+)$")
_CHOOSE_ONE = re.compile(r"^(?:(.*?), )?choose one —$")
_CHOOSE_OTHER = re.compile(r"\bchoose (?:two|three|one or more|one or both|any number)\b")
_BULLET = re.compile(r"^•\s*(.+)$")

# Verbs that start a new clause after "and".
_VERBS = (
    "deal|deals|gain|gains|lose|loses|draw|draws|discard|discards|destroy|exile|return|"
    "tap|untap|add|create|get|gets|put|sacrifice|sacrifices|mill|mills|counter|have|has"
)
_AND_VERB = re.compile(rf" and (?=(?:you )?(?:{_VERBS})\b)")
_STARTS_WITH_VERB = re.compile(rf"^(?:{_VERBS})\b")
_SUBJECT = re.compile(rf"^(.*?)\b(?:{_VERBS})\b")

# Words that make a clause depend on something the DSL cannot state: an
# option, a condition, a back-reference, another zone, a count.
_UNREADABLE = (
    (r"\bmay\b", "optional effect (may)"),
    (r"\bif\b|\bunless\b|\bonly\b|\bexcept\b", "condition"),
    (r"\bup to\b", "up to"),
    (r"\banother\b", "another"),
    (r"\bit\b|\bits\b(?! owner)|\bthat\b|\bthose\b|\bthey\b|\bthem\b|\btheir\b(?! owners)", "back-reference"),
    (r"\bgraveyard\b|\blibrary\b|\bbattlefield\b|\bhand\b(?<!owner's hand)(?<!owners' hands)", "another zone"),
    (r"\bnumber of\b|\bequal to\b|\bhalf\b|\btwice\b", "computed amount"),
    (r"\bthis turn\b|\bnext\b|\bduring\b", "duration"),
    (r"\bwhere\b|\brather\b|\badditional\b|\brandom\b|\bchosen\b|\bnamed?\b", "choice or condition"),
    (r"\bdefending player\b|\battacking\b|\bblocking\b|\bblocked\b", "combat reference"),
)

_NUMBER = re.compile(r"\b(\d+|x|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b")


class SplitError(ValueError):
    """A line the splitter cannot read; nothing is asked about such a card."""

    def __init__(self, line: str, reason: str):
        super().__init__(f"{reason}: “{line}”")
        self.line = line
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Clause:
    text: str
    # Numbers stated in the clause ("a"/"an" count as 1, "x" stays "X").
    amounts: tuple[int | str, ...]
    # Selector phrases found in the clause, in order.
    target_phrases: tuple[str, ...]
    # Index (within the ability) of the clause whose subject this one repeats:
    # "target creature gets +2/+2 and gains flying" -> both affect one target.
    shares_target_with: int | None = None


@dataclass(frozen=True, slots=True)
class ClauseSet:
    """One ability, as read from one oracle line (or one mode of it)."""

    line: str
    trigger: str
    clauses: tuple[Clause, ...]
    cost: Cost | None = None
    mode_group: int | None = None


def _candidates(text: str) -> tuple[tuple, tuple]:
    amounts = []
    for word in _NUMBER.findall(text):
        value = "X" if word == "x" else 1 if word in ("a", "an") else number_value(word)
        if value not in amounts:
            amounts.append(value)
    # Look for phrases in the clause with the cost of the selector table; a
    # phrase inside a longer one ("you" in "you control") is not counted twice.
    phrases = [m.group(0) for m in re.finditer(rf"\b(?:{selector_phrase_regex()})\b", text)]
    return tuple(amounts), tuple(phrases)


def _parse_cost(text: str, line: str) -> Cost:
    mana = ""
    tap = sacrifice = False
    life = 0
    for part in (p.strip() for p in text.split(",")):
        if part == "{t}":
            tap = True
        elif re.fullmatch(r"pay (\d+) life", part):
            life = int(part.split()[1])
        elif re.fullmatch(r"sacrifice this (?:creature|artifact|enchantment|land|permanent)", part):
            sacrifice = True
        elif re.fullmatch(r"(?:\{[^}]+\})+", part) and not mana:
            try:
                parse_mana_cost(part)
            except ManaCostError as exc:
                raise SplitError(line, str(exc)) from exc
            mana = part.upper()
        else:
            raise SplitError(line, f"unsupported cost “{part}”")
    return Cost(mana=mana, tap=tap, sacrifice_self=sacrifice, life=life)


def split_effect(effect: str, line: str) -> tuple[Clause, ...]:
    """Cuts one effect into atomic clauses, keeping their textual order."""
    effect = effect.strip().rstrip(".").strip()
    pieces: list[tuple[str, int | None]] = []
    whole: set[int] = set()
    for sentence in re.split(r"\.\s+", effect):
        sentence = re.sub(r"^then\s+", "", sentence.strip())
        if re.fullmatch(r"shuffle(?: your library)?", sentence) and whole:
            continue  # the search before it already shuffles
        if sentence.startswith("search your library"):
            # One clause, read whole by the search grammar: its commas and
            # "it" belong to it, and the shuffle is part of the operation.
            whole.add(len(pieces))
            pieces.append((re.sub(r",? then shuffle(?: your library)?$", "", sentence), None))
            continue
        for part in re.split(r",? then ", sentence):
            part = part.strip().strip(",").strip()
            if not part:
                continue
            halves = _AND_VERB.split(part)
            start = len(pieces)
            pieces.append((halves[0], None))
            subject_match = _SUBJECT.match(halves[0])
            subject = subject_match.group(1).strip() if subject_match else ""
            for half in halves[1:]:
                if subject and _STARTS_WITH_VERB.match(half):
                    pieces.append((f"{subject} {half}", start))
                else:
                    pieces.append((half, None))
            # "gets +2/+2 and gains flying until end of turn": the duration
            # at the end covers every half.
            if len(halves) > 1 and halves[-1].endswith(" until end of turn"):
                pieces[start:] = [
                    (text if text.endswith(" until end of turn") else text + " until end of turn", share)
                    for text, share in pieces[start:]
                ]

    if not pieces:
        raise SplitError(line, "empty effect")
    if len(pieces) > MAX_CLAUSES_PER_ABILITY:
        raise SplitError(line, f"more than {MAX_CLAUSES_PER_ABILITY} clauses")

    clauses = []
    for index, (text, share) in enumerate(pieces):
        lenient = index in whole
        if not lenient and ("," in text or ";" in text or ":" in text):
            raise SplitError(line, f"unreadable clause “{text}”")
        for pattern, reason in _UNREADABLE:
            if lenient and reason in ("back-reference", "another zone"):
                continue
            if re.search(pattern, text):
                raise SplitError(line, reason)
        amounts, phrases = _candidates(text)
        clauses.append(Clause(text, amounts, phrases, share))
    return tuple(clauses)


def _ability(line: str, facts: CardFacts, mode_group: int | None = None, trigger=None, effect=None):
    if trigger is None:
        trigger, cost, effect = _read_line(line, facts)
    else:
        cost = None
    return ClauseSet(line, trigger, split_effect(effect, line), cost, mode_group)


def _read_line(line: str, facts: CardFacts) -> tuple[str, Cost | None, str]:
    for pattern, trigger in _TRIGGERS:
        match = pattern.match(line)
        if match:
            return trigger, None, match.group(1)
    if _OTHER_TRIGGER.match(line):
        raise SplitError(line, "unsupported trigger")
    activated = _ACTIVATED.match(line)
    if activated:
        if re.match(r"add\b", activated.group(2)):
            # Mana is paid automatically from free {T} sources only: a mana
            # ability with a cost would compile but never be used.
            raise SplitError(line, "mana abilities with a cost are not supported")
        return Trigger.ACTIVATED, _parse_cost(activated.group(1), line), activated.group(2)
    if facts.is_instant_or_sorcery:
        return Trigger.SPELL, None, line
    return Trigger.STATIC, None, line


def split_abilities(residual: str, facts: CardFacts) -> list[ClauseSet]:
    """Reads ``facts``' residual text into abilities and atomic clauses.

    Raises :class:`SplitError` naming the first line it cannot read.
    """
    lines = [line.strip() for line in residual.splitlines() if line.strip()]
    abilities: list[ClauseSet] = []
    mode_groups = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if _CHOOSE_OTHER.search(line):
            raise SplitError(line, "only “choose one” modes are supported")
        choose = _CHOOSE_ONE.match(line)
        if choose:
            bullets = []
            index += 1
            while index < len(lines) and _BULLET.match(lines[index]):
                bullets.append(_BULLET.match(lines[index]).group(1))
                index += 1
            if len(bullets) < 2:
                raise SplitError(line, "a modal ability needs at least two modes")
            if choose.group(1) or not facts.is_instant_or_sorcery:
                # Only a spell's modes are chosen (on cast); a triggered ability
                # would need a choice the kernel does not make.
                raise SplitError(line, "modes are only supported on instants and sorceries")
            trigger = Trigger.SPELL
            for bullet in bullets:
                abilities.append(_ability(bullet, facts, mode_groups, trigger, bullet))
            mode_groups += 1
            continue
        if _BULLET.match(line):
            raise SplitError(line, "a mode outside “choose one”")
        abilities.append(_ability(line, facts))
        index += 1

    if len(abilities) > MAX_ABILITIES:
        raise SplitError(residual, f"more than {MAX_ABILITIES} abilities")
    return abilities
