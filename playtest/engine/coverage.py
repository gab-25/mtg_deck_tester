"""How much of a deck the playtest actually simulates.

Weighted by quantity, not by unique name: thirty-five Forests are thirty-five
supported cards. Two warnings matter more than the percentage — an unsupported
commander, and dead spells (unsupported instants and sorceries, which sit in
hand as blanks).
"""

from dataclasses import dataclass

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    name: str
    quantity: int
    is_commander: bool
    status: str
    reason_kind: str = ""
    reason: str = ""
    is_instant_or_sorcery: bool = False


@dataclass(frozen=True, slots=True)
class CoverageReport:
    total: int
    supported: int
    partial: int
    unsupported: int
    # Supported and partial cards, as a whole percentage of the deck (floored).
    percent: int
    commander_unsupported: bool
    dead_spells: int
    # Unsupported quantity per reason kind, sorted by kind.
    reasons: dict[str, int]
    # Unsupported cards, sorted by name.
    unsupported_cards: tuple[CoverageEntry, ...]


def coverage_report(entries: list[CoverageEntry]) -> CoverageReport:
    def count(status):
        return sum(e.quantity for e in entries if e.status == status)

    total = sum(e.quantity for e in entries)
    unsupported = [e for e in entries if e.status == UNSUPPORTED]
    reasons: dict[str, int] = {}
    for entry in unsupported:
        reasons[entry.reason_kind] = reasons.get(entry.reason_kind, 0) + entry.quantity
    return CoverageReport(
        total=total,
        supported=count(SUPPORTED),
        partial=count(PARTIAL),
        unsupported=count(UNSUPPORTED),
        percent=(count(SUPPORTED) + count(PARTIAL)) * 100 // total if total else 0,
        commander_unsupported=any(e.is_commander for e in unsupported),
        dead_spells=sum(e.quantity for e in unsupported if e.is_instant_or_sorcery),
        reasons=dict(sorted(reasons.items())),
        unsupported_cards=tuple(sorted(unsupported, key=lambda e: e.name)),
    )
