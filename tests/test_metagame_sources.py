"""Tests for the metagame sources, on recorded pages (no network)."""

import json
from pathlib import Path

import pytest

from decks.metagame import SourceError, edhtop16, mtgtop8, source_for
from decks.rules.decklist import parse_decklist_text

FIXTURES = Path(__file__).parent / "fixtures" / "metagame"


class _Response:
    def __init__(self, status_code=200, content=b"", payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload


def _file(name):
    return (FIXTURES / name).read_bytes()


def _mtgtop8_get(url, headers, timeout):
    if "mtgo?d=" in url:
        name = "mtgtop8_mtgo_EDH.txt" if url.endswith("898986") else "mtgtop8_mtgo_MO.txt"
        return _Response(content=_file(name))
    pages = {
        "format?f=MO": "mtgtop8_format_MO.html",
        "format?f=EDH": "mtgtop8_format_EDH.html",
        "cEDH_decks?f=EDH": "mtgtop8_breakdown_EDH.html",
        "f=EDH": "mtgtop8_archetype_EDH.html",
        "archetype?a=": "mtgtop8_archetype_MO.html",
    }
    for key, name in pages.items():
        if key in url:
            return _Response(content=_file(name))
    return _Response(404)


def test_edhtop16_reads_commanders_partners_and_full_decklists():
    payload = json.loads(_file("edhtop16_commanders.json"))
    sent = {}

    def post(url, json, headers, timeout):
        sent.update(json)
        return _Response(payload=payload)

    entries = edhtop16.fetch("commander", 2, post=post)
    assert sent["variables"] == {"first": 2}
    first = entries[0]
    assert first.archetype == "Kraum, Ludevic's Opus / Tymna the Weaver"
    assert first.rank == 1 and 2 < first.meta_share < 3
    cards = parse_decklist_text(first.decklist, "commander")
    assert [c["name"] for c in cards if c["is_commander"]] == ["Kraum, Ludevic's Opus", "Tymna the Weaver"]
    assert sum(c["quantity"] for c in cards) == 100


def test_mtgtop8_reads_constructed_archetypes_with_their_share():
    entries = mtgtop8.fetch("modern", 2, get=_mtgtop8_get, sleep=lambda s: None)
    assert [(e.archetype, e.meta_share, e.rank) for e in entries] == [("UR Aggro", 8.0, 1), ("4/5c Aggro", 7.0, 2)]
    cards = parse_decklist_text(entries[0].decklist, "modern")
    assert sum(c["quantity"] for c in cards if c["board"] == "main") == 60
    assert any(c["board"] == "side" for c in cards)


def test_mtgtop8_reads_the_duel_commander_metagame_breakdown():
    urls = []

    def get(url, headers, timeout):
        urls.append(url)
        return _mtgtop8_get(url, headers, timeout)

    entries = mtgtop8.fetch("duel", 2, get=get, sleep=lambda s: None)
    assert [(e.archetype, e.meta_share, e.rank) for e in entries] == [
        ("Brigid, Clachan's Heart", 6.05, 1),
        ("Cloud, Midgar Mercenary", 5.76, 2),
    ]
    # The breakdown is loaded the way the page's script loads it, for the page's period.
    assert any("cEDH_decks?f=EDH&show=pop" in u and "meta=115" in u for u in urls)
    cards = parse_decklist_text(entries[0].decklist, "duel")
    assert [c["name"] for c in cards if c["is_commander"]] == ["Brigid, Clachan's Heart"]
    assert sum(c["quantity"] for c in cards) == 100


@pytest.mark.parametrize("fmt", ["modern", "duel"])
def test_a_failing_page_is_a_source_error(fmt):
    with pytest.raises(SourceError):
        mtgtop8.fetch(fmt, 3, get=lambda url, headers, timeout: _Response(500), sleep=lambda s: None)


def test_edhtop16_errors_are_source_errors():
    with pytest.raises(SourceError):
        edhtop16.fetch("commander", 3, post=lambda *a, **k: _Response(502))


def test_each_format_has_its_source():
    assert source_for("commander") is edhtop16
    assert all(source_for(f) is mtgtop8 for f in ("duel", "standard", "pioneer", "modern", "legacy", "vintage", "pauper"))
