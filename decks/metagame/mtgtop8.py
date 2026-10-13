"""MTGTop8: the top decks of Duel Commander and the constructed formats.

Each format page has a metagame breakdown (archetypes with their share): for
each archetype, the first listed deck is taken. Constructed formats render it in
the page; Duel Commander loads it with a script from ``cEDH_decks`` (shares in
per mille), which is requested the same way. Decklists come from MTGTop8's MTGO
export, which ``robots.txt`` allows (MTGGoldfish's download does not).
"""

import html
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
# The Duel Commander breakdown: one row per commander, its share in per mille.
_BREAKDOWN_ROW = re.compile(
    r'<a href=["]?(archetype\?a=\d+&meta=\d+&f=EDH[^ >"]*)["]?>([^<]+)</a></div>'
    r'.{0,600}?class=S14 align=center>([\d.]+) <span class=O14>&permil;',
    re.S,
)
# The period the page shows by default (its first ``meta`` value).
_DEFAULT_META = re.compile(r"\bmeta\s*=\s*\"?(\d+)")


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
    if fmt == "duel":
        meta = (_DEFAULT_META.findall(page) or ["115"])[0]
        breakdown = client.text(
            f"cEDH_decks?f=EDH&show=pop&cid=&meta={meta}&gamerid1=&gamerid2=&cEDH_cp=1"
        )
        archetypes = [
            (path, name, float(per_mille) / 10) for path, name, per_mille in _BREAKDOWN_ROW.findall(breakdown)
        ]
    else:
        archetypes = [(path, name, float(share)) for path, name, share in _ARCHETYPE.findall(page)]

    entries = []
    for rank, (path, name, share) in enumerate(archetypes[:limit], start=1):
        refs = _DECK_REF.findall(client.text(path))
        if not refs:
            continue
        export = client.text(f"mtgo?d={refs[0]}")
        entries.append(
            MetagameEntry(
                archetype=html.unescape(name).strip(),
                source_url=f"{BASE_URL}/{path}",
                decklist=_duel_decklist(export) if fmt == "duel" else export,
                meta_share=round(share, 2),
                rank=rank,
            )
        )
    if not entries:
        raise SourceError(f"MTGTop8 listed no {fmt} decks: the page layout may have changed.")
    return entries
