"""The match summary: numbers computed from the stored events, never written by a model.

:func:`summarize_match` is pure and deterministic — the same events give the
same dict, every run. It reads Forge's game log (``Turn 3 (Red)``, ``Red cast
Lightning Bolt …``, ``Life: Green 20 > 17``, ``Red played Mountain (31)``) by
the names the seats played under, and the bridge's decision events, so it can
say how much of a seat's game its agent actually played: every decision the
bridge does not expose (mana payment, damage assignment, scry…) and every
fallback is Forge's AI.
"""

import re
import statistics

from decks.formats import FORMATS

LIFE_CHANGE = re.compile(r" (-?\d+) > (-?\d+)$")
TURN_LINE = re.compile(r"^Turn (\d+) \((.+)\)$")


def table_names(seats: list) -> list[str]:
    """The name each seat plays under: its deck's, plus the seat when a deck sits twice."""
    names = [seat.deck_name for seat in seats]
    return [
        f"{name} (seat {seat.position + 1})" if names.count(name) > 1 else name
        for name, seat in zip(names, seats)
    ]


def event_row(event):
    """Accepts stored ``MatchEvent`` rows and bridge event dicts alike."""
    if isinstance(event, dict):
        return event.get("turn") or 0, event["kind"], event.get("seat"), event.get("text") or "", event.get("payload") or {}
    return event.turn, event.kind, event.seat_position, event.text, event.payload or {}


def _by_prefix(text: str, names: list[tuple[str, int]], prefix: str = "") -> tuple[int, str] | None:
    """``(seat, rest)`` when ``text`` is ``prefix + name + " " + rest``; longest names first."""
    for name, seat in names:
        head = f"{prefix}{name} "
        if text.startswith(head):
            return seat, text[len(head):]
    return None


def fallback_executions(rows) -> set[int]:
    """The indexes of the ``forge_ai`` events that carry out a fallback.

    When a decision falls back, Forge's AI takes it and the bridge logs that
    too: the seat's next event after a ``fallback`` is its ``forge_ai``
    twin. It is the same decision, so it is counted (and shown) once.
    """
    paired, waiting = set(), {}
    for index, (_, kind, seat, _, _) in enumerate(rows):
        if kind == "fallback":
            waiting[seat] = True
        elif kind == "forge_ai" and waiting.pop(seat, False):
            paired.add(index)
        elif kind == "decision":
            waiting.pop(seat, None)
    return paired


def fallback_reason(payload: dict) -> str:
    """Why Forge's AI took a decision: the agent's reason when it delegated, else the bridge's."""
    reason = payload.get("reason") or "delegate"
    if reason == "delegate":
        return payload.get("fallback_kind") or "delegate"
    return reason


def summarize_match(events, seats: list, fmt: str, game: dict) -> dict:
    """The JSON-safe summary of a game.

    ``events`` are the match's events in order, ``seats`` its ``Seat`` rows,
    ``fmt`` the format key and ``game`` the bridge's final game
    (``status``, ``winner``, ``end_reason``, ``turn``).
    """
    rows = [event_row(e) for e in events]
    names = table_names(seats)
    by_length = sorted(((name, seat.position) for name, seat in zip(names, seats)), key=lambda x: -len(x[0]))
    starting = FORMATS[fmt].starting_life
    winner = game.get("winner")
    turns = max([game.get("turn") or 0] + [turn for turn, *_ in rows])

    stats = {
        seat.position: {
            "seat": seat.position,
            "name": name,
            "agent": seat.agent,
            "result": "draw" if winner is None else ("won" if winner == seat.position else "lost"),
            "final_life": starting,
            "life_by_turn": [],
            "turns_taken": 0,
            "spells_cast": 0,
            "lands_played": 0,
            "unsupported_cards": len((seat.coverage or {}).get("unsupported_cards") or []),
            "decisions": {"agent": 0, "forge_ai": 0, "agent_share": None},
            "confidence": {"rated": 0, "median": None, "low": 0},
            "fallbacks": {},
        }
        for seat, name in zip(seats, names)
    }
    life = {position: starting for position in stats}
    confidences: dict[int, list[float]] = {position: [] for position in stats}
    automatic: dict[str, int] = {}
    current_turn = 0
    executions = fallback_executions(rows)

    def close_turns(upto: int) -> None:
        for seat in stats.values():
            while len(seat["life_by_turn"]) < upto:
                seat["life_by_turn"].append(life[seat["seat"]])

    for index, (turn, kind, seat, text, payload) in enumerate(rows):
        if index in executions:
            continue
        if turn > current_turn:
            close_turns(turn - 1)
            current_turn = turn
        if kind == "log":
            category = payload.get("type")
            if category == "LIFE":
                found = _by_prefix(text, by_length, "Life: ")
                change = LIFE_CHANGE.search(text)
                if found and change:
                    life[found[0]] = int(change.group(2))
            elif category == "TURN":
                line = TURN_LINE.match(text)
                active = next((s for n, s in by_length if line and n == line.group(2)), None)
                if active is not None:
                    stats[active]["turns_taken"] += 1
            elif category == "STACK_ADD":
                found = _by_prefix(text, by_length)
                if found and found[1].startswith("cast "):
                    stats[found[0]]["spells_cast"] += 1
            elif category == "LAND":
                found = _by_prefix(text, by_length)
                if found and found[1].startswith("played "):
                    stats[found[0]]["lands_played"] += 1
        elif seat not in stats:
            continue
        elif kind == "decision":
            stats[seat]["decisions"]["agent"] += 1
            if payload.get("confidence") is not None:
                confidences[seat].append(payload["confidence"])
        elif kind == "fallback":
            stats[seat]["decisions"]["forge_ai"] += 1
            reason = fallback_reason(payload)
            stats[seat]["fallbacks"][reason] = stats[seat]["fallbacks"].get(reason, 0) + 1
            if reason == "low_confidence":
                stats[seat]["confidence"]["low"] += 1
            if payload.get("confidence") is not None:
                confidences[seat].append(payload["confidence"])
        elif kind == "forge_ai":
            stats[seat]["decisions"]["forge_ai"] += 1
            method = payload.get("method") or "other"
            automatic[method] = automatic.get(method, 0) + 1
    close_turns(turns)

    for seat in stats.values():
        position = seat["seat"]
        seat["final_life"] = life[position]
        decisions = seat["decisions"]
        total = decisions["agent"] + decisions["forge_ai"]
        if total:
            decisions["agent_share"] = round(100 * decisions["agent"] / total)
        rated = confidences[position]
        seat["confidence"]["rated"] = len(rated)
        if rated:
            seat["confidence"]["median"] = round(statistics.median(rated), 3)
        seat["fallbacks"] = dict(sorted(seat["fallbacks"].items()))

    return {
        "winner_seat": winner,
        "end_reason": game.get("end_reason") or "",
        "turns": turns,
        "starting_life": starting,
        "seats": [stats[seat.position] for seat in seats],
        # What Forge's AI decided because the bridge does not expose it, by Forge method.
        "automatic": dict(sorted(automatic.items())),
    }
