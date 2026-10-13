"""Metagame decks: the top decks of each format, from public sources.

Each source module exposes ``fetch(fmt, limit, *, get, post, sleep) ->
list[MetagameEntry]`` and raises :class:`SourceError` when the source cannot be
read. Sources are only called by ``manage.py sync_metagame``, never during a
match.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetagameEntry:
    archetype: str
    source_url: str
    # Plain-text decklist the importer reads (Commander / Deck / Sideboard headers).
    decklist: str
    # Percent of the format's metagame, when the source gives one.
    meta_share: float | None = None
    rank: int = 0


class SourceError(Exception):
    """A metagame source could not be reached or read."""


def source_for(fmt: str):
    """The source module that serves ``fmt``: EDHTop16 for Commander, MTGTop8 otherwise."""
    from . import edhtop16, mtgtop8

    return edhtop16 if fmt == "commander" else mtgtop8
