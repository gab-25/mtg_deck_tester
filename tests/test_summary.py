"""Tests for the match summary, on a game the real bridge played (tests/fixtures/forge_game.json)."""

import json
from pathlib import Path
from types import SimpleNamespace

from playtest.summary import fallback_reason, summarize_match

GAME = json.loads((Path(__file__).parent / "fixtures" / "forge_game.json").read_text())


def _seats(*agents):
    return [
        SimpleNamespace(position=position, deck_name=name, agent=agent, coverage={"unsupported_cards": []})
        for position, (name, agent) in enumerate(zip(("Red", "Green"), agents))
    ]


def test_the_summary_reads_forges_log_by_the_seats_names():
    summary = summarize_match(GAME["events"], _seats("random", "random"), "modern", GAME["game"])
    red, green = summary["seats"]
    assert summary["winner_seat"] == 1 and summary["end_reason"] == "AllOpponentsLost" and summary["turns"] == 14
    assert (red["result"], green["result"]) == ("lost", "won")
    assert (red["final_life"], green["final_life"]) == (-6, 8)
    assert red["turns_taken"] == green["turns_taken"] == 7
    # "Red triggered …" lines are abilities, not spells.
    assert (red["spells_cast"], green["spells_cast"]) == (5, 10)
    assert red["lands_played"] + green["lands_played"] == 5
    assert len(red["life_by_turn"]) == 14
    assert red["life_by_turn"][0] == 20 and green["life_by_turn"][0] == 17


def test_decisions_are_split_between_the_agent_and_forges_ai():
    summary = summarize_match(GAME["events"], _seats("random", "random"), "modern", GAME["game"])
    red, green = summary["seats"]
    assert red["decisions"] == {"agent": 20, "forge_ai": 2, "agent_share": 91}
    assert green["decisions"] == {"agent": 42, "forge_ai": 3, "agent_share": 93}
    assert summary["automatic"] == {"assignCombatDamage": 5}


def test_confidence_and_fallbacks_by_reason():
    events = [
        {"seq": 0, "turn": 1, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"confidence": 0.9}},
        {"seq": 1, "turn": 1, "seat": 0, "kind": "decision", "text": "priority: Pass priority", "payload": {"confidence": 0.5}},
        {"seq": 2, "turn": 1, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "low_confidence", "confidence": 0.1}},
        {"seq": 3, "turn": 2, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "timeout"}},
        {"seq": 4, "turn": 2, "seat": 0, "kind": "fallback", "text": "", "payload": {"reason": "delegate", "fallback_kind": "error"}},
    ]
    seat = summarize_match(events, _seats("jev", "forge_ai"), "duel", {"turn": 2})["seats"][0]
    assert seat["confidence"] == {"rated": 3, "median": 0.5, "low": 1}
    assert seat["fallbacks"] == {"error": 1, "low_confidence": 1, "timeout": 1}
    assert seat["decisions"]["agent_share"] == 40
    assert seat["result"] == "draw"


def test_a_fallbacks_reason_is_the_agents_when_it_delegated():
    assert fallback_reason({"reason": "delegate", "fallback_kind": "budget"}) == "budget"
    assert fallback_reason({"reason": "delegate"}) == "delegate"
    assert fallback_reason({"reason": "too_many_options"}) == "too_many_options"


def test_the_summary_is_identical_for_identical_events():
    seats = _seats("random", "jev")
    first = summarize_match(GAME["events"], seats, "modern", GAME["game"])
    second = summarize_match(json.loads(json.dumps(GAME["events"])), seats, "modern", dict(GAME["game"]))
    assert first == second
    assert json.loads(json.dumps(first)) == first
