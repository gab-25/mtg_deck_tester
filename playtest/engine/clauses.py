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
from .dsl import OPS, Cost, Trigger, parse_clause, selector_phrase_regex
from .manacost import ManaCostError, parse_mana_cost

MAX_CLAUSES_PER_ABILITY = 4
MAX_ABILITIES = 8

_ENTERS = r"enters?(?: the battlefield)?"
_TO_GRAVEYARD = r"is put into a graveyard from the battlefield"
_CAST_WHO = {"you cast": "YOU", "an opponent casts": "OPPONENT", "a player casts": "ANY"}
_CAST_WHAT = {
    "spell": "SPELL",
    "noncreature spell": "NONCREATURE",
    "creature spell": "CREATURE",
    "instant or sorcery spell": "INSTANT_OR_SORCERY",
    "artifact spell": "ARTIFACT",
    "colorless spell": "COLORLESS",
}
_ENTERS_WHAT = {"another creature": "OTHER_CREATURE", "a creature": "CREATURE", "a land": "LAND"}

# (pattern, triggers): each trigger is (trigger, when); "when" may name a group.
_TRIGGERS = (
    (rf"^when(?:ever)? this \w+ {_ENTERS} or attacks, (?P<effect>.+)$", ((Trigger.ETB, ""), (Trigger.ATTACKS, ""))),
    (rf"^when(?:ever)? this \w+ {_ENTERS} or {_TO_GRAVEYARD}, (?P<effect>.+)$", ((Trigger.ETB, ""), (Trigger.DIES, ""))),
    (rf"^when(?:ever)? this \w+ {_ENTERS}, (?P<effect>.+)$", ((Trigger.ETB, ""),)),
    (rf"^when(?:ever)? this \w+ (?:dies|{_TO_GRAVEYARD}), (?P<effect>.+)$", ((Trigger.DIES, ""),)),
    (r"^whenever this \w+ attacks, (?P<effect>.+)$", ((Trigger.ATTACKS, ""),)),
    (r"^at the beginning of your upkeep, (?P<effect>.+)$", ((Trigger.UPKEEP, ""),)),
    (r"^at the beginning of your end step, (?P<effect>.+)$", ((Trigger.END_STEP, ""),)),
    (
        r"^whenever this \w+ deals combat damage to a player(?: or (?:planeswalker|battle))?, (?P<effect>.+)$",
        ((Trigger.COMBAT_DAMAGE, ""),),
    ),
    (
        rf"^whenever (?P<who>{'|'.join(_CAST_WHO)}) an? (?P<what>{'|'.join(_CAST_WHAT)}), (?P<effect>.+)$",
        ((Trigger.CAST, "cast"),),
    ),
    (
        rf"^whenever (?P<kind>{'|'.join(_ENTERS_WHAT)}) you control {_ENTERS}, (?P<effect>.+)$",
        ((Trigger.ENTERS, "enters"),),
    ),
    (r"^whenever you attack, (?P<effect>.+)$", ((Trigger.YOU_ATTACK, ""),)),
    (r"^at the beginning of combat on your turn, (?P<effect>.+)$", ((Trigger.BEGIN_COMBAT, ""),)),
    (r"^when(?:ever)? this \w+ leaves the battlefield, (?P<effect>.+)$", ((Trigger.LEAVES, ""),)),
    (r"^whenever you gain life, (?P<effect>.+)$", ((Trigger.GAIN_LIFE, ""),)),
    (r"^whenever an opponent draws a card, (?P<effect>.+)$", ((Trigger.OPPONENT_DRAWS, ""),)),
)
_TRIGGERS = tuple((re.compile(pattern), triggers) for pattern, triggers in _TRIGGERS)
# An ability word ("landfall —") has no rules meaning (CR 207.2c).
_ABILITY_WORD = re.compile(r"^[a-z' ]+ — (?=(?:when|whenever|at) )")
_OTHER_TRIGGER = re.compile(r"^(?:when|whenever|at the beginning|at end)\b")
_ACTIVATED = re.compile(r"^([^:\"]+): (.+)$")
_CHOOSE_ONE = re.compile(r"^(?:(.*?), )?choose one —$")
_CHOOSE_OTHER = re.compile(r"\bchoose (?:two|three|one or more|one or both|any number)\b")
_BULLET = re.compile(r"^•\s*(.+)$")

# Verbs that start a new clause after "and".
_VERBS = (
    "deal|deals|gain|gains|lose|loses|draw|draws|discard|discards|destroy|exile|return|"
    "tap|untap|add|create|get|gets|put|sacrifice|sacrifices|mill|mills|counter|have|has|"
    "scry|surveil|investigate|look"
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
    # "you may …": its controller chooses whether it happens; "if you do, …":
    # it happens only if the last optional clause before it did.
    optional: bool = False
    if_done: bool = False


@dataclass(frozen=True, slots=True)
class ClauseSet:
    """One ability, as read from one oracle line (or one mode of it)."""

    line: str
    trigger: str
    clauses: tuple[Clause, ...]
    cost: Cost | None = None
    mode_group: int | None = None
    # The event filter of CAST / ENTERS triggers.
    when: str = ""


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
    life = discard = 0
    for part in (p.strip() for p in text.split(",")):
        if part == "{t}":
            tap = True
        elif re.fullmatch(r"discard (?:a|one|two|three) cards?", part):
            discard = number_value(part.split()[1]) or 1
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
    return Cost(mana=mana, tap=tap, sacrifice_self=sacrifice, life=life, discard=discard)


def reads_whole(text: str) -> bool:
    """Whether some operation's grammar reads ``text`` as one clause."""
    return any(parse_clause(op, text) is not None for op in OPS)


# How many sentences one grammar may span ("target player reveals their hand.
# you choose … that player discards that card").
_MAX_SPAN = 3


def _sentences(effect: str) -> list[str]:
    """The effect's sentences, with runs that one grammar reads whole joined into one."""
    sentences = [re.sub(r"^then\s+", "", s.strip()) for s in re.split(r"\.\s+", effect)]
    joined = []
    index = 0
    while index < len(sentences):
        for end in range(min(len(sentences), index + _MAX_SPAN), index + 1, -1):
            text = ". ".join(sentences[index:end])
            if reads_whole(text):
                joined.append(text)
                index = end
                break
        else:
            joined.append(sentences[index])
            index += 1
    return joined


def split_effect(effect: str, line: str) -> tuple[Clause, ...]:
    """Cuts one effect into atomic clauses, keeping their textual order.

    A sentence (or run of sentences) that an operation's grammar reads whole
    stays one clause: the grammar states exactly what its references, zones
    and options mean, so the word filters below do not apply to it.
    """
    effect = effect.strip().rstrip(".").strip()
    pieces: list[tuple[str, int | None]] = []
    whole: set[int] = set()
    flags: dict[int, tuple[bool, bool]] = {}
    for sentence in _sentences(effect):
        start = len(pieces)
        if_done = sentence.startswith("if you do, ")
        sentence = sentence.removeprefix("if you do, ")
        rest = re.sub(r",? then shuffle(?: your library)?$", "", sentence.removeprefix("you may "))
        optional = sentence.startswith("you may ") and reads_whole(rest)
        sentence = sentence.removeprefix("you may ") if optional else sentence
        _split_sentence(sentence, pieces, whole)
        for index in range(start, len(pieces)):
            flags[index] = (optional and index == start, if_done)

    if not pieces:
        raise SplitError(line, "empty effect")
    if len(pieces) > MAX_CLAUSES_PER_ABILITY:
        raise SplitError(line, f"more than {MAX_CLAUSES_PER_ABILITY} clauses")

    clauses = []
    for index, (text, share) in enumerate(pieces):
        lenient = index in whole or reads_whole(text)
        if not lenient and ("," in text or ";" in text or ":" in text):
            raise SplitError(line, f"unreadable clause “{text}”")
        for pattern, reason in () if lenient else _UNREADABLE:
            if re.search(pattern, text):
                raise SplitError(line, reason)
        amounts, phrases = _candidates(text)
        optional, if_done = flags[index]
        clauses.append(Clause(text, amounts, phrases, share, optional, if_done))
    return tuple(clauses)


def _split_sentence(sentence: str, pieces: list, whole: set) -> None:
    """Cuts one sentence into clauses, appending ``(text, shares_target_with)`` to ``pieces``."""
    if re.fullmatch(r"shuffle(?: your library)?", sentence) and whole:
        return  # the search before it already shuffles
    if sentence.startswith("search your library"):
        # One clause, read whole by the search grammar: its commas and
        # "it" belong to it, and the shuffle is part of the operation.
        whole.add(len(pieces))
        pieces.append((re.sub(r",? then shuffle(?: your library)?$", "", sentence), None))
        return
    if reads_whole(sentence):
        whole.add(len(pieces))
        pieces.append((sentence, None))
        return
    for part in re.split(r",? then ", sentence):
        part = part.strip().strip(",").strip()
        if not part:
            continue
        if reads_whole(part):
            whole.add(len(pieces))
            pieces.append((part, None))
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


def _abilities(line: str, facts: CardFacts) -> list[ClauseSet]:
    """One line's abilities: one, or two when one effect has two triggers ("enters or attacks")."""
    triggers, cost, effect = _read_line(line, facts)
    clauses = split_effect(effect, line)
    return [ClauseSet(line, trigger, clauses, cost, None, when) for trigger, when in triggers]


def _when(kind: str, match: re.Match) -> str:
    if kind == "cast":
        return f"{_CAST_WHO[match.group('who')]}:{_CAST_WHAT[match.group('what')]}"
    if kind == "enters":
        return _ENTERS_WHAT[match.group("kind")]
    return ""


def _read_line(line: str, facts: CardFacts) -> tuple[tuple, Cost | None, str]:
    for pattern, triggers in _TRIGGERS:
        match = pattern.match(line)
        if match:
            return tuple((trigger, _when(kind, match)) for trigger, kind in triggers), None, match.group("effect")
    if _OTHER_TRIGGER.match(line):
        raise SplitError(line, "unsupported trigger")
    activated = _ACTIVATED.match(line)
    if activated:
        if re.match(r"add\b", activated.group(2)):
            # Mana is paid automatically from free {T} sources only: a mana
            # ability with a cost would compile but never be used.
            raise SplitError(line, "mana abilities with a cost are not supported")
        return ((Trigger.ACTIVATED, ""),), _parse_cost(activated.group(1), line), activated.group(2)
    if facts.is_instant_or_sorcery:
        return ((Trigger.SPELL, ""),), None, line
    return ((Trigger.STATIC, ""),), None, line


def split_abilities(residual: str, facts: CardFacts) -> list[ClauseSet]:
    """Reads ``facts``' residual text into abilities and atomic clauses.

    Raises :class:`SplitError` naming the first line it cannot read.
    """
    lines = [_ABILITY_WORD.sub("", line.strip()) for line in residual.splitlines() if line.strip()]
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
                abilities.append(ClauseSet(bullet, trigger, split_effect(bullet, bullet), None, mode_groups))
            mode_groups += 1
            continue
        if _BULLET.match(line):
            raise SplitError(line, "a mode outside “choose one”")
        abilities += _abilities(line, facts)
        index += 1

    if len(abilities) > MAX_ABILITIES:
        raise SplitError(residual, f"more than {MAX_ABILITIES} abilities")
    return abilities
