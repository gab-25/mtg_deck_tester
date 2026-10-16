"""Exporting a match for an offline review (#76)."""

import json

import pytest
from django.core.management import CommandError, call_command

from playtest import export

from .test_traces import _played, jev, user  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def test_jsonl_is_every_event_in_order_with_its_trace(user, fake_forge, jev):
    match = _played(user)
    lines = [json.loads(line) for line in export.match_jsonl(match).splitlines()]
    assert [line["seq"] for line in lines] == list(match.events.values_list("seq", flat=True))
    decisions = [line for line in lines if line["kind"] == "decision"]
    assert decisions and all(line["trace"]["options"] and line["trace"]["state"] for line in decisions)
    assert all("trace" not in line for line in lines if line["kind"] == "log")


def test_jsonl_for_one_seat_and_only_flagged(user, fake_forge, jev):
    fake_forge.failing_cast = True
    match = _played(user)
    seat = [json.loads(line) for line in export.match_jsonl(match, seat=1).splitlines()]
    assert seat and all(line["seat"] in (1, None) for line in seat)
    flagged = [json.loads(line) for line in export.match_jsonl(match, flagged=True).splitlines()]
    assert flagged and all(line["trace"]["flags"] for line in flagged)


def test_markdown_narrates_each_turn_and_decision(user, fake_forge, jev):
    match = _played(user)
    text = export.match_markdown(match)
    assert text.startswith("# Deck 0 vs Deck 1")
    assert "## Turn 1" in text and "Keep this hand" in text and "→" in text


def test_a_match_without_traces_still_exports(user, fake_forge, jev, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_CALLS", "0")
    match = _played(user)
    assert export.match_jsonl(match) and "## Turn 1" in export.match_markdown(match)


def test_the_commands_write_files(user, fake_forge, jev, tmp_path, capsys):
    match = _played(user)
    call_command("export_match", str(match.id), "--format", "md")
    assert "## Turn 1" in capsys.readouterr().out
    call_command("export_playtest", str(match.playtest_id), "--format", "jsonl", "--output", str(tmp_path))
    [written] = list(tmp_path.iterdir())
    assert written.name == export.file_name(match, "jsonl") and written.read_text()


def test_an_unknown_id_is_a_command_error():
    for command in ("export_match", "export_playtest"):
        with pytest.raises(CommandError):
            call_command(command, "not-a-uuid", "--output", "/nonexistent")
