"""EDHTop16: the most played competitive Commander (cEDH) decks.

One GraphQL request: the most popular commanders of the last three months, with
their meta share and the decklist of their best-placed tournament entry.
"""

import time

import requests

from ..scryfall import SCRYFALL_HEADERS
from . import MetagameEntry, SourceError

API_URL = "https://edhtop16.com/api/graphql"
SITE_URL = "https://edhtop16.com"
REQUEST_TIMEOUT = 30
SOURCE = "edhtop16"

QUERY = """query Top($first: Int!) { commanders(first: $first, sortBy: POPULARITY, timePeriod: THREE_MONTHS) { edges { node { name stats(filters: {timePeriod: THREE_MONTHS}) { metaShare } entries(first: 1, sortBy: TOP) { edges { node { decklist maindeck { name } } } } } } } }"""


def _decklist(commander: str, cards: list[str]) -> str:
    lines = ["Commander"] + [f"1 {name.strip()}" for name in commander.split(" / ")]
    lines += ["", "Deck"] + [f"1 {name}" for name in cards]
    return "\n".join(lines) + "\n"


def fetch(fmt: str, limit: int, *, get=None, post=None, sleep=time.sleep) -> list[MetagameEntry]:
    if fmt != "commander":
        raise SourceError(f"EDHTop16 has no {fmt} decks.")
    post = post or requests.post
    try:
        response = post(
            API_URL,
            json={"query": QUERY, "variables": {"first": limit}},
            headers={**SCRYFALL_HEADERS, "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SourceError(f"EDHTop16 could not be reached: {exc}") from exc
    if response.status_code != 200:
        raise SourceError(f"EDHTop16 returned HTTP {response.status_code}.")
    try:
        edges = response.json()["data"]["commanders"]["edges"]
    except (ValueError, LookupError, TypeError) as exc:
        raise SourceError(f"Unexpected EDHTop16 response ({exc}).") from exc

    entries = []
    for rank, edge in enumerate(edges, start=1):
        node = edge.get("node") or {}
        top = ((node.get("entries") or {}).get("edges") or [{}])[0].get("node") or {}
        cards = [card["name"] for card in top.get("maindeck") or [] if card.get("name")]
        if not node.get("name") or not cards:
            continue
        share = (node.get("stats") or {}).get("metaShare")
        entries.append(
            MetagameEntry(
                archetype=node["name"],
                source_url=top.get("decklist") or SITE_URL,
                decklist=_decklist(node["name"], cards),
                meta_share=round(share * 100, 2) if share is not None else None,
                rank=rank,
            )
        )
    if not entries:
        raise SourceError("EDHTop16 returned no decks.")
    return entries
