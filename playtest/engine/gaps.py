"""Measuring what keeps cards out of the DSL.

Compilation stops at a card's first problem; to decide what to extend next we
need *every* problem of every card. :func:`card_gaps` lists them without asking
the judge: the pre-pass blocklist, the clause splitter run on each ability on its
own, and clauses no operation's grammar reads. An empty list means the card is
compilable if the judge agrees.

Pure: no Django, no I/O.
"""

import re

from .cardrules import ResidualClass, characteristics
from .clauses import SplitError, split_abilities
from .dsl import OPS, parse_clause

_BULLET = re.compile(r"^•")
_CHOOSE = re.compile(r"\bchoose (?:one|two|three|one or more|one or both|any number)\b.*—$")


def ability_blocks(residual: str) -> list[str]:
    """The residual text cut into abilities: one per line, modes kept with their header."""
    blocks: list[list[str]] = []
    for line in (l.strip() for l in residual.splitlines() if l.strip()):
        if _BULLET.match(line) and blocks and _CHOOSE.search(blocks[-1][0]):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(block) for block in blocks]


def normalize(reason: str) -> str:
    """Folds numbers so similar clauses rank together."""
    return re.sub(r"\b\d+\b", "N", reason)


def _split_reason(exc: SplitError) -> str:
    if exc.reason == "unsupported trigger":
        return f"unsupported trigger: {exc.line.split(',')[0].strip()}"
    return exc.reason


def card_gaps(card: dict) -> list[str]:
    """Every reason ``card`` cannot be compiled, in text order, without duplicates."""
    facts = characteristics(card)
    gaps: list[str] = list(facts.block_reasons)
    if facts.residual_class == ResidualClass.EMPTY:
        return gaps
    for block in ability_blocks(facts.residual):
        try:
            abilities = split_abilities(block, facts)
        except SplitError as exc:
            gaps.append(_split_reason(exc))
            continue
        for ability in abilities:
            for clause in ability.clauses:
                if not any(parse_clause(op, clause.text) for op in OPS):
                    gaps.append(normalize(f"no operation reads: {clause.text}"))
    return list(dict.fromkeys(gaps))
