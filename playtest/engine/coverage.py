"""Coverage: how much of a deck the playtest can actually simulate.

Weighted by quantity, not by unique name: twelve unsupported Islands would be
twelve cards. Two warnings are first-class, because they change what a match
result means: an unsupported commander, and the number of dead spells
(unsupported instants and sorceries, which sit in hand uncastable).

Pure: no Django, no I/O.
"""

from dataclasses import dataclass


class Status:
    SUPPORTED = "supported"
    # Supported, but only the front face of a multi-faced card is played.
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ReasonKind:
    """Why a card is unsupported — grouped so a stronger judge can be sized (#44)."""

    BLOCKLISTED = "blocklisted"
    UNREADABLE = "unreadable"
    NONE = "none"
    UNSTABLE = "unstable"
    LOW_CONFIDENCE = "low_confidence"
    INVALID = "invalid"
    NOT_ASKED = "not_asked"


REASON_LABELS = {
    ReasonKind.BLOCKLISTED: "Mechanic outside the DSL (blocklisted)",
    ReasonKind.UNREADABLE: "Rules text the splitter cannot read",
    ReasonKind.NONE: "No operation or selector fits",
    ReasonKind.UNSTABLE: "Answer changed with the option order",
    ReasonKind.LOW_CONFIDENCE: "Judge not confident enough",
    ReasonKind.INVALID: "Program rejected by the validator",
    ReasonKind.NOT_ASKED: "Judge not reachable (retried next time)",
}


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    name: str
    quantity: int
    is_commander: bool
    status: str
    reason_kind: str = ""
    reason: str = ""
    # An instant or sorcery: unsupported, it is a dead card in hand.
    spell: bool = False


def coverage_report(entries: list[CoverageEntry]) -> dict:
    """The JSON-safe coverage report of one deck."""
    total = sum(e.quantity for e in entries)
    counts = {Status.SUPPORTED: 0, Status.PARTIAL: 0, Status.UNSUPPORTED: 0}
    reasons: dict[str, int] = {}
    unsupported = []
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + entry.quantity
        if entry.status == Status.UNSUPPORTED:
            reasons[entry.reason_kind] = reasons.get(entry.reason_kind, 0) + entry.quantity
            unsupported.append(
                {
                    "name": entry.name,
                    "quantity": entry.quantity,
                    "commander": entry.is_commander,
                    "spell": entry.spell,
                    "kind": entry.reason_kind,
                    "reason": entry.reason,
                }
            )
    playable = counts[Status.SUPPORTED] + counts[Status.PARTIAL]
    unsupported.sort(key=lambda u: (not u["commander"], not u["spell"], u["name"]))
    return {
        "total": total,
        "supported": counts[Status.SUPPORTED],
        "partial": counts[Status.PARTIAL],
        "unsupported": counts[Status.UNSUPPORTED],
        # Integer percent, rounded down: 99.9% coverage is not 100%.
        "percent": playable * 100 // total if total else 100,
        "commander_unsupported": any(
            e.is_commander and e.status == Status.UNSUPPORTED for e in entries
        ),
        "dead_spells": sum(
            e.quantity for e in entries if e.spell and e.status == Status.UNSUPPORTED
        ),
        "reasons": [
            {"kind": kind, "label": REASON_LABELS.get(kind, kind), "count": count}
            for kind, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "unsupported_cards": unsupported,
    }
