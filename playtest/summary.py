"""The match summary: numbers computed from the events, never written by a model.

:func:`summarize_match` is pure and deterministic — the same events give the
same dict, every run — and its caveats are data: each seat's coverage, an
unsupported commander, and the fact that mana is paid automatically, so
nobody credits an agent with decisions it never made.
"""

from .engine.events import EventKind

SPELL_TYPES = ("creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker", "battle")


def _event(recorded):
    """Accepts engine ``RecordedEvent``s and stored ``MatchEvent`` rows alike."""
    if hasattr(recorded, "event"):
        e = recorded.event
        return recorded.round, e.kind, e.seat, e.payload
    return recorded.round, recorded.kind, recorded.seat_position, recorded.payload


def _primary_type(types) -> str:
    return next((t for t in SPELL_TYPES if t in types), "other")


def summarize_match(events, coverage: list[dict], result, *, has_commander: bool = True) -> dict:
    """The JSON-safe summary of a finished game.

    ``events`` are the game's recorded events, ``coverage`` each seat's coverage
    report (in seat order), ``result`` the :class:`~.engine.game.GameResult`.
    """
    rows = [_event(e) for e in events]
    setup = next((p for _, kind, _, p in rows if kind == EventKind.SETUP), {})
    over = next((p for _, kind, _, p in reversed(rows) if kind == EventKind.GAME_OVER), {})
    names = setup.get("decks") or [f"Seat {i + 1}" for i in range(len(result.seats))]
    starting = setup.get("starting_life", 0)
    final = {s["seat"]: s for s in over.get("seats", [])}

    seats = []
    for seat, name in enumerate(names):
        seats.append(
            {
                "seat": seat,
                "name": name,
                "life_by_round": [],
                "damage_dealt": {"combat": 0, "commander": 0, "effects": 0},
                "spells_cast": {},
                "commander_casts": 0,
                "commander_tax_paid": 0,
                "lands_played": 0,
                "dead_spells_drawn": final.get(seat, {}).get("dead_spells_drawn", 0),
                "unsupported_skipped": 0,
                "agent": {
                    "decisions": final.get(seat, {}).get("decisions", 0),
                    "rated_decisions": 0,
                    "mean_confidence": None,
                    "low_confidence_fallbacks": 0,
                    "error_fallbacks": 0,
                },
                "coverage": {
                    "percent": (coverage[seat] if seat < len(coverage) else {}).get("percent"),
                    "commander_unsupported": (coverage[seat] if seat < len(coverage) else {}).get("commander_unsupported", False),
                    "dead_spells": (coverage[seat] if seat < len(coverage) else {}).get("dead_spells", 0),
                },
                "eliminated_round": None,
            }
        )

    life = {seat: starting for seat in range(len(names))}
    confidences: dict[int, list[float]] = {seat: [] for seat in range(len(names))}
    current_round = 0

    def close_rounds(upto: int) -> None:
        for _ in range(len(seats[0]["life_by_round"]) + 1, upto + 1):
            for seat in seats:
                seat["life_by_round"].append(life[seat["seat"]])

    for round_no, kind, seat, payload in rows:
        if round_no > current_round:
            close_rounds(round_no - 1)
            current_round = round_no
        if kind == EventKind.DAMAGE and "target_seat" in payload:
            target, source = payload["target_seat"], payload.get("source_seat")
            life[target] = payload.get("life", life[target] - payload["amount"])
            if source is not None and source != target:
                bucket = "commander" if payload.get("commander") else "combat" if payload.get("combat") else "effects"
                seats[source]["damage_dealt"][bucket] += payload["amount"]
        elif kind == EventKind.EFFECT and payload.get("effect") in ("lose_life", "gain_life"):
            life[seat] = payload.get("life", life[seat])
            source = payload.get("source_seat")
            if payload["effect"] == "lose_life" and source is not None and source != seat:
                seats[source]["damage_dealt"]["effects"] += payload.get("amount", 0)
        elif kind == EventKind.CAST:
            spell = _primary_type(payload.get("types", ()))
            cast = seats[seat]["spells_cast"]
            cast[spell] = cast.get(spell, 0) + 1
            if payload.get("commander"):
                seats[seat]["commander_casts"] += 1
                seats[seat]["commander_tax_paid"] += payload.get("tax", 0)
        elif kind == EventKind.PLAY_LAND:
            seats[seat]["lands_played"] += 1
        elif kind == EventKind.UNSUPPORTED_SKIPPED:
            seats[seat]["unsupported_skipped"] += 1
        elif kind == EventKind.ELIMINATED:
            seats[seat]["eliminated_round"] = round_no
        elif kind in (EventKind.DECISION, EventKind.PASS) and payload.get("confidence") is not None:
            confidences[seat].append(payload["confidence"])
        elif kind == EventKind.AGENT_FALLBACK:
            agent = seats[seat]["agent"]
            if payload.get("fallback_kind") == "low_confidence":
                agent["low_confidence_fallbacks"] += 1
                if payload.get("confidence") is not None:
                    confidences[seat].append(payload["confidence"])
            else:
                agent["error_fallbacks"] += 1
    close_rounds(result.rounds)

    for seat in seats:
        rated = confidences[seat["seat"]]
        seat["agent"]["rated_decisions"] = len(rated)
        if rated:
            seat["agent"]["mean_confidence"] = round(sum(rated) / len(rated), 3)
        seat["spells_cast"] = dict(sorted(seat["spells_cast"].items()))
        seat["final_life"] = final.get(seat["seat"], {}).get("life", life[seat["seat"]])

    return {
        "has_commander": has_commander,
        "winner_seat": result.winner_seat,
        "end_reason": result.end_reason,
        "rounds": result.rounds,
        "seats": seats,
        "caveats": {
            "mana_paid_automatically": True,
            "commander_unsupported": [s["seat"] for s in seats if s["coverage"]["commander_unsupported"]],
            "lowest_coverage": min((s["coverage"]["percent"] for s in seats if s["coverage"]["percent"] is not None), default=None),
        },
    }
