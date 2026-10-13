"""Parsing of plain-text Commander decklists."""

import re

from ..formats import DEFAULT_FORMAT, FORMATS

# Section headers that introduce the commander(s).
_COMMANDER_HEADERS = {"commander", "commanders"}

# Every other header exports carry. The cards below them are part of the deck
# all the same — Commander has no sideboard, so anything pasted in counts
# towards the 100 cards and is flagged by the legality check if it overflows.
_MAIN_HEADERS = {
    "deck",
    "main",
    "mainboard",
    "sideboard",
    "maybeboard",
    "companion",
    "considering",
}

_SECTION_HEADERS = _COMMANDER_HEADERS | _MAIN_HEADERS

# In constructed formats the sideboard is its own zone, and the "maybe" lists
# are notes, not cards. Arena lists the companion twice — on its own and in the
# sideboard, where it actually lives — so its own section is skipped too.
_SIDEBOARD_HEADERS = {"sideboard"}
_IGNORED_HEADERS = {"maybeboard", "considering", "companion"}

# "4 Lightning Bolt" and the "4x Lightning Bolt" spelling used by several
# deckbuilding sites.
_CARD_PATTERN = re.compile(r"^(\d+)\s*[xX]?\s+(.+?)$")

# Inline commander marker appended by Moxfield/Archidekt exports, e.g.
# "1 Atraxa, Praetors' Voice *CMDR*".
_COMMANDER_MARKER = re.compile(r"\s*\*\s*(?:CMDR|COMMANDER)\s*\*\s*$", re.IGNORECASE)


def parse_decklist_text(text: str, fmt: str = DEFAULT_FORMAT) -> list:
    """Parses raw decklist text into ``{quantity, name, is_commander}`` dicts.

    Commanders are the cards listed under a ``Commander`` section header or
    tagged with a trailing ``*CMDR*`` marker. In a Commander format every other
    card belongs to the deck, whatever its header. In a constructed format the
    cards also carry ``board`` — ``"side"`` under a ``Sideboard`` header (or,
    in a list with no headers at all, after the first blank line), ``"main"``
    otherwise — and the maybeboard and companion sections are skipped.
    """
    constructed = not FORMATS.get(fmt, FORMATS[DEFAULT_FORMAT]).has_commander
    # MTGO and MTGGoldfish exports have no headers: the sideboard follows the
    # main deck after a blank line.
    headerless = constructed and not any(
        line.strip().lower().rstrip(":") in _SECTION_HEADERS for line in text.splitlines()
    )
    cards = []
    in_commander_section = False
    board = "main"
    skipping = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            if headerless and cards:
                board = "side"
            continue
        # Ignore comments.
        if line.startswith("//") or line.startswith("#"):
            continue

        # A bare section header switches the section the following cards land in.
        header = line.lower().rstrip(":")
        if header in _SECTION_HEADERS:
            in_commander_section = header in _COMMANDER_HEADERS
            board = "side" if header in _SIDEBOARD_HEADERS else "main"
            skipping = constructed and header in _IGNORED_HEADERS
            continue
        if skipping:
            continue

        line, marked = _strip_commander_marker(line)

        match = _CARD_PATTERN.match(line)
        if match:
            qty = int(match.group(1))
            name = match.group(2).strip()
        else:
            # Fallback for lines without a quantity prefix.
            qty = 1
            name = line

        if not name:
            continue

        entry = {"quantity": qty, "name": name, "is_commander": marked or in_commander_section}
        if constructed:
            entry["board"] = board
        cards.append(entry)

    return cards


def _strip_commander_marker(line: str) -> tuple:
    """Splits a trailing ``*CMDR*`` marker off a card line.

    Returns ``(line_without_marker, was_marked)``.
    """
    stripped = _COMMANDER_MARKER.sub("", line)
    return stripped.strip(), stripped != line

