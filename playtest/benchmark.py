"""A repeatable measure of how Jev plays (#79): Jev against Forge's AI with a match's two decks.

:func:`create_benchmark` copies a match's decks into two playtest sessions,
one per deck in Jev's hands: in each match Jev plays seat 0 and Forge's AI
the other deck in seat 1, on the same seeds in both sessions. Seat 0 is the
"deck under test" of every page, so the sessions' records are Jev's.

:func:`report` reads the finished matches' summaries — never a model — and
gives the numbers to compare before and after a change: Jev's record, how
much of its game it played itself, its confidence, the checks' flags per
game and its agreement with Forge's AI.
"""

import statistics
from dataclasses import dataclass, field

from django.db import transaction

from .models import Match, Playtest, Seat
from .stats import RESULTS


def create_benchmark(source: Match, games: int, seed: int) -> list[Playtest]:
    """Two sessions of ``games`` pending matches, seeds ``seed`` … ``seed + games - 1``."""
    seats = list(source.seats.order_by("position"))
    if len(seats) != 2:
        raise ValueError(f"A benchmark needs a two-seat match; this one has {len(seats)} seats.")
    playtests = []
    with transaction.atomic():
        for jev, forge_ai in (seats, seats[::-1]):
            playtest = Playtest.objects.create(
                owner=source.owner, format=source.format, deck=jev.deck, deck_name=jev.deck_name,
                seed=seed, games_per_opponent=games,
            )
            for game in range(games):
                match = Match.objects.create(owner=source.owner, playtest=playtest, format=source.format, seed=seed + game)
                Seat.objects.bulk_create([
                    _copy(jev, match, 0, Seat.Agent.JEV),
                    _copy(forge_ai, match, 1, Seat.Agent.FORGE_AI),
                ])
            playtests.append(playtest)
    return playtests


def _copy(seat: Seat, match: Match, position: int, agent: str) -> Seat:
    return Seat(match=match, position=position, deck=seat.deck, metagame_deck=seat.metagame_deck,
                deck_name=seat.deck_name, agent=agent)


def queue_order(playtests: list[Playtest]) -> list[Match]:
    """The matches game by game across the sessions, so both get going together."""
    rows = [list(playtest.matches.order_by("seed")) for playtest in playtests]
    return [match for game in zip(*rows) for match in game]


@dataclass
class Report:
    """Jev's numbers over the finished matches of some sessions (seat 0 of each)."""

    name: str
    games: int = 0
    unfinished: int = 0
    won: int = 0
    lost: int = 0
    draw: int = 0
    turns: list[int] = field(default_factory=list)
    agent: int = 0
    forge_ai: int = 0
    fallbacks: dict[str, int] = field(default_factory=dict)
    medians: list[float] = field(default_factory=list)
    flags: dict[str, int] = field(default_factory=dict)
    rated: int = 0
    agree: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)

    @property
    def win_rate(self) -> int | None:
        return round(100 * self.won / self.games) if self.games else None

    @property
    def avg_turns(self) -> float | None:
        return round(sum(self.turns) / len(self.turns), 1) if self.turns else None

    @property
    def jev_share(self) -> int | None:
        total = self.agent + self.forge_ai
        return round(100 * self.agent / total) if total else None

    @property
    def median_confidence(self) -> float | None:
        return round(statistics.median(self.medians), 2) if self.medians else None

    @property
    def flags_per_game(self) -> float | None:
        return round(sum(self.flags.values()) / self.games, 1) if self.games else None

    @property
    def agreement(self) -> int | None:
        return round(100 * self.agree / self.rated) if self.rated else None


def _add(counts: dict, more: dict) -> None:
    for key, value in more.items():
        counts[key] = counts.get(key, 0) + value


def report(name: str, matches) -> Report:
    result = Report(name)
    for match in matches:
        if match.result not in RESULTS:
            result.unfinished += 1
            continue
        summary = match.summary or {}
        jev = next((seat for seat in summary.get("seats") or [] if seat.get("seat") == 0), {})
        result.games += 1
        setattr(result, match.result, getattr(result, match.result) + 1)
        result.turns.append(summary.get("turns") or match.turn)
        decisions = jev.get("decisions") or {}
        result.agent += decisions.get("agent") or 0
        result.forge_ai += decisions.get("forge_ai") or 0
        _add(result.fallbacks, jev.get("fallbacks") or {})
        if (median := (jev.get("confidence") or {}).get("median")) is not None:
            result.medians.append(median)
        _add(result.flags, jev.get("flags") or {})
        agreement = jev.get("agreement") or {}
        result.rated += agreement.get("rated") or 0
        result.agree += agreement.get("agree") or 0
        for kind, (rated, agree) in (agreement.get("by_kind") or {}).items():
            pair = result.by_kind.setdefault(kind, [0, 0])
            pair[0] += rated
            pair[1] += agree
    return result


def _cell(value, suffix="") -> str:
    return "—" if value is None else f"{value}{suffix}"


def table(reports: list[Report]) -> str:
    """The reports side by side, one column each, as plain text."""
    rows = [
        ("Games", [str(r.games) + (f" (+{r.unfinished} unfinished)" if r.unfinished else "") for r in reports]),
        ("Jev won–lost–draw", [f"{r.won}–{r.lost}–{r.draw}" for r in reports]),
        ("Jev win rate", [_cell(r.win_rate, "%") for r in reports]),
        ("Average turns", [_cell(r.avg_turns) for r in reports]),
        ("Decisions Jev took", [_cell(r.jev_share, "%") for r in reports]),
        ("Median confidence", [_cell(r.median_confidence) for r in reports]),
        ("Flags per game", [_cell(r.flags_per_game) for r in reports]),
        ("Agrees with Forge AI", [_cell(r.agreement, "%") + (f" of {r.rated}" if r.rated else "") for r in reports]),
    ]
    for key in sorted({k for r in reports for k in r.fallbacks}):
        rows.append((f"  fallback: {key}", [str(r.fallbacks.get(key, 0)) for r in reports]))
    for key in sorted({k for r in reports for k in r.flags}):
        rows.append((f"  flag: {key}", [str(r.flags.get(key, 0)) for r in reports]))
    for kind in sorted({k for r in reports for k in r.by_kind}):
        cells = []
        for r in reports:
            rated, agree = r.by_kind.get(kind, (0, 0))
            cells.append(f"{round(100 * agree / rated)}% of {rated}" if rated else "—")
        rows.append((f"  agrees on {kind}", cells))
    header = [""] + [r.name for r in reports]
    lines = [header] + [[label] + cells for label, cells in rows]
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in lines) + "\n"
