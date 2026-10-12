"""MTGTop8: the top decks of Duel Commander and the constructed formats.

Constructed formats have a metagame page (archetypes with their share): for each
archetype, the first listed deck is taken. Duel Commander has none, so the
best-placed deck of each of the latest events is taken instead. Decklists come from MTGTop8's
MTGO export, which ``robots.txt`` allows (MTGGoldfish's download does not).
"""

import re
import time

import requests

from ..scryfall import SCRYFALL_HEADERS
from . import MetagameEntry, SourceError

BASE_URL = "https://www.mtgtop8.com"
REQUEST_TIMEOUT = 20
# Polite delay between requests.
REQUEST_DELAY = 0.5
SOURCE = "mtgtop8"

CODES = {
    "standard": "ST",
    "pioneer": "PI",
    "modern": "MO",
    "legacy": "LE",
    "vintage": "VI",
    "pauper": "PAU",
    "duel": "EDH",
}

_ARCHETYPE = re.compile(
    r'<a href=["]?(archetype\?a=\d+&meta=\d+&f=[A-Z]+)["]?>([^<]+)</a></div>\s*<div>\s*<div[^>]*>(\d+) %'
)
_DECK_REF = re.compile(r"name=deck_ref\[\d+\] value=(\d+)")
_EVENT = re.compile(r'href=["]?event\?e=(\d+)&f=EDH')
# One row per deck of an event, in placement order: the rank cell, then the deck link.
_EVENT_ROW = re.compile(r'align=center class=S14>([^<]*)</div>')
_ROW_DECK = re.compile(r'd=(\d+)&f=EDH["]?>([^<]+)</a>')


class _Client:
    def __init__(self, get, sleep):
        self.get = get or requests.get
        self.sleep = sleep
        self.first = True

    def text(self, path: str) -> str:
        if not self.first:
            self.sleep(REQUEST_DELAY)
        self.first = False
        url = f"{BASE_URL}/{path}"
        try:
            response = self.get(url, headers=SCRYFALL_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"MTGTop8 could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(f"MTGTop8 returned HTTP {response.status_code} for {url}.")
        content = response.content
        return content.decode("latin-1") if isinstance(content, bytes) else str(content)


def _duel_decklist(export: str) -> str:
    """MTGO exports put a Commander deck's commander under ``Sideboard``."""
    return re.sub(r"(?m)^Sideboard\s*$", "Commander", export)


def fetch(fmt: str, limit: int, *, get=None, post=None, sleep=time.sleep) -> list[MetagameEntry]:
    code = CODES.get(fmt)
    if code is None:
        raise SourceError(f"MTGTop8 has no {fmt} decks.")
    client = _Client(get, sleep)
    page = client.text(f"format?f={code}")
    entries = []
    if fmt == "duel":
        events = list(dict.fromkeys(_EVENT.findall(page)))
        for event_id in events:
            if len(entries) >= limit:
                break
            event = client.text(f"event?e={event_id}&f=EDH")
            top = None
            for row in _EVENT_ROW.finditer(event):
                top = _ROW_DECK.search(event, row.end(), row.end() + 600)
                if top:
                    break
            if top is None:
                continue
            deck_id, commander = top.groups()
            export = client.text(f"mtgo?d={deck_id}")
            entries.append(
                MetagameEntry(
                    archetype=commander.strip(),
                    source_url=f"{BASE_URL}/event?e={event_id}&d={deck_id}&f=EDH",
                    decklist=_duel_decklist(export),
                    rank=len(entries) + 1,
                )
            )
    else:
        archetypes = _ARCHETYPE.findall(page)[:limit]
        for rank, (path, name, share) in enumerate(archetypes, start=1):
            refs = _DECK_REF.findall(client.text(path))
            if not refs:
                continue
            export = client.text(f"mtgo?d={refs[0]}")
            entries.append(
                MetagameEntry(
                    archetype=name.strip(),
                    source_url=f"{BASE_URL}/{path}",
                    decklist=export,
                    meta_share=float(share),
                    rank=rank,
                )
            )
    if not entries:
        raise SourceError(f"MTGTop8 listed no {fmt} decks: the page layout may have changed.")
    return entries
