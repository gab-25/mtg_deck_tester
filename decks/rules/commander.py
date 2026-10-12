"""Commander (EDH) format rules: color identity and deck legality.

Pure functions, no I/O. A deck is checked in two passes, because the rules need
different data:

* :func:`check_decklist` runs on the parsed decklist entries (names only), so
  the web form can reject an illegal deck instantly.
* :func:`check_deck` runs on the *processed cards* — the ``{"quantity",
  "is_commander", "data"}`` dicts built from Scryfall data — and covers the
  rules that need the real card (commander eligibility, color identity, and
  the ban list of the deck's format).
"""

import re

from ..formats import DEFAULT_FORMAT, FORMATS
from ..text_utils import front_face_name
from .cards import front_type_line, is_basic_land, rules_text

# Commander deck-construction rules (Comprehensive Rules 903).
# A deck is exactly 100 cards, commander included.
COMMANDER_DECK_SIZE = 100
# One commander, or two when they form a partner pair (Partner, Partner with,
# Friends forever, or a commander that chooses a Background and that Background).
MAX_COMMANDERS = 2

# Cards whose rules text lifts the singleton restriction (Relentless Rats,
# Shadowborn Apostle, Persistent Petitioners, Dragon's Approach, ...).
UNLIMITED_COPIES_TEXT = "a deck can have any number of cards named"

# The singleton rule is checked twice: once on the pasted decklist, where only
# card *names* are known, and once on the fetched cards, where the rules text
# above settles it. These two lists back the by-name pass.
BASIC_LAND_NAMES = frozenset(
    name.lower()
    for base in ("Plains", "Island", "Swamp", "Mountain", "Forest")
    for name in (base, f"Snow-Covered {base}")
) | {"wastes", "snow-covered wastes"}

# Cards printed with "A deck can have any number of cards named ...". A finite,
# hand-maintained list: it only has to cover the by-name pass, since the fetched
# card's rules text is authoritative afterwards. The few cards that cap the
# allowance (Seven Dwarves, Nazgûl) are treated as unlimited — the cap is not
# enforced.
ANY_NUMBER_CARD_NAMES = frozenset(
    name.lower()
    for name in (
        "Relentless Rats",
        "Rat Colony",
        "Shadowborn Apostle",
        "Persistent Petitioners",
        "Dragon's Approach",
        "Slime Against Humanity",
        "Seven Dwarves",
        "Nazgûl",
    )
)

# Rules text that lets a non-legendary-creature card be a commander
# (planeswalkers such as Rowan, Scion of War; backgrounds; etc.).
CAN_BE_COMMANDER_TEXT = "can be your commander"

# Canonical color order (Magic's "WUBRG" wheel).
WUBRG = "WUBRG"

_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def card_color_identity(card_data: dict) -> list:
    """Returns a card's color identity as WUBRG letters, in canonical order.

    Prefers Scryfall's ``color_identity``; decks stored before it was persisted
    fall back to the colors of the mana symbols printed on their faces (a close
    approximation that misses only off-color rules text).
    """
    identity = card_data.get("color_identity")
    if identity is None:
        found = set()
        for face in card_data.get("faces", []):
            for symbol in _MANA_SYMBOL_RE.findall(face.get("mana_cost") or ""):
                found.update(ch for ch in symbol if ch in WUBRG)
        identity = found
    return [c for c in WUBRG if c in identity]


def commanders(processed_cards: list) -> list:
    """Returns the cards flagged as commanders, in decklist order."""
    return [item for item in processed_cards if item.get("is_commander")]


def commander_names(processed_cards: list) -> list:
    """Returns the names of the deck's commander(s)."""
    return [item["data"].get("name", "") for item in commanders(processed_cards)]


def deck_color_identity(processed_cards: list) -> list:
    """Returns the deck's color identity: the union of its commanders' colors.

    With no commander declared there is nothing to derive the identity from, so
    the whole deck's colors are used instead — which is what the deck would play
    anyway.
    """
    source = commanders(processed_cards) or processed_cards
    found = set()
    for item in source:
        found.update(card_color_identity(item["data"]))
    return [c for c in WUBRG if c in found]


def _can_be_commander(card_data: dict) -> bool:
    """Reports whether a card may be designated as a commander."""
    # The front face is the one that goes in the command zone: a card that
    # merely transforms into a legendary creature (Bloodline Keeper) is not a
    # legal commander.
    type_line = front_type_line(card_data)
    if "legendary" in type_line and "creature" in type_line:
        return True
    # A Background is a commander only next to one that chooses it
    # (checked by partner_issue).
    if "background" in type_line:
        return True
    return CAN_BE_COMMANDER_TEXT in rules_text(card_data)


def _is_background(card_data: dict) -> bool:
    type_line = front_type_line(card_data)
    return "background" in type_line and "creature" not in type_line


def _has_keyword_line(text: str, keyword: str) -> bool:
    return any(
        line.strip() == keyword or line.strip().startswith(keyword + " (") for line in text.splitlines()
    )


def partner_issue(cmdrs: list) -> str | None:
    """Why two commanders cannot share a deck, or None when they can (CR 702.124)."""
    if len(cmdrs) != 2:
        return None
    a, b = (item["data"] for item in cmdrs)
    names = [front_face_name(card.get("name", "")).lower() for card in (a, b)]
    texts = [rules_text(a), rules_text(b)]
    if all(_has_keyword_line(t, "partner") for t in texts):
        return None
    if f"partner with {names[1]}" in texts[0] and f"partner with {names[0]}" in texts[1]:
        return None
    if all(_has_keyword_line(t, "friends forever") for t in texts):
        return None
    for chooser, other in ((a, b), (b, a)):
        if "choose a background" in rules_text(chooser) and "background" in front_type_line(other):
            return None
    return (
        f"“{a.get('name', 'Unknown card')}” and “{b.get('name', 'Unknown card')}” cannot share "
        "a deck: two commanders must be a partner pair (Partner, Partner with, Friends "
        "forever, or a Background)."
    )


def _allows_duplicates(card_data: dict) -> bool:
    """Reports whether a card is exempt from the singleton rule."""
    return is_basic_land(card_data) or UNLIMITED_COPIES_TEXT in rules_text(card_data)


def _format_identity(identity: list) -> str:
    """Renders a color identity for a message: ``{W}{U}`` or ``colorless``."""
    return "".join(f"{{{c}}}" for c in identity) if identity else "colorless"


def _size_issue(total: int) -> list:
    """The 100-card rule, shared by both passes."""
    if total == COMMANDER_DECK_SIZE:
        return []
    return [
        f"The deck has {total} cards: Commander requires exactly "
        f"{COMMANDER_DECK_SIZE}, commander included."
    ]


def _commander_count_issues(count: int) -> list:
    """The "one commander, or a pair" rule, shared by both passes."""
    if count == 0:
        return [
            "No commander declared: list it under a “Commander” header or tag "
            "its line with *CMDR*."
        ]
    if count > MAX_COMMANDERS:
        return [
            f"{count} commanders declared: a deck has one commander, or two "
            "that form a partner pair."
        ]
    return []


def _singleton_issue(name: str, qty: int) -> list:
    """The singleton rule's message for one entry."""
    return [
        f"{qty}× “{name}”: Commander is singleton, only one copy of each card "
        "is allowed (basic lands aside)."
    ]


def _tally(entries) -> dict:
    """Totals ``(key, card, quantity)`` triples per card, in first-seen order.

    Returns ``{key: (card, total_quantity)}``. One card can take up several
    decklist lines — twice under the same name, or once per spelling of a
    double-faced card — and the singleton rule counts copies, not lines.
    """
    totals = {}
    for key, card, qty in entries:
        known, total = totals.get(key, (card, 0))
        totals[key] = (known, total + qty)
    return totals


def check_decklist(entries: list) -> list:
    """Checks the parsed decklist — names and quantities only.

    Returns every problem that can be spotted without fetching the cards, so the
    creation form can reject an illegal deck on the spot instead of spending
    minutes on Scryfall first. An empty list means nothing is wrong *yet*:
    :func:`check_deck` still has the last word.
    """
    issues = _size_issue(sum(entry["quantity"] for entry in entries))
    issues += _commander_count_issues(
        sum(1 for entry in entries if entry.get("is_commander"))
    )

    totals = _tally(
        (front_face_name(entry["name"]).lower(), entry["name"], entry["quantity"])
        for entry in entries
    )
    for key, (name, qty) in totals.items():
        if qty > 1 and key not in BASIC_LAND_NAMES | ANY_NUMBER_CARD_NAMES:
            issues += _singleton_issue(name, qty)

    return issues


def check_deck(processed_cards: list, fmt: str = DEFAULT_FORMAT) -> list:
    """Checks the fetched deck against the deck-construction rules of ``fmt``.

    Re-runs the structural checks on the real cards (quantities can only be
    trusted once every name has resolved) and adds the rules that need Scryfall
    data: commander eligibility, color identity and the ban list. Only the ban
    list varies with the format — construction is identical across them.
    Returns every problem at once, so the caller can report them all together.
    """
    issues = _size_issue(sum(item["quantity"] for item in processed_cards))

    cmdrs = commanders(processed_cards)
    issues += _commander_count_issues(len(cmdrs))

    if len(cmdrs) == 2:
        issue = partner_issue(cmdrs)
        if issue:
            issues.append(issue)
    elif any(_is_background(item["data"]) for item in cmdrs):
        issues.append("A Background is only a commander next to one that says “Choose a Background”.")

    for item in cmdrs:
        if not _can_be_commander(item["data"]):
            name = item["data"].get("name", "Unknown card")
            issues.append(
                f"“{name}” cannot be a commander: it is neither a legendary "
                "creature nor a card that says it can be your commander."
            )

    # The Scryfall id is the same for both spellings of a double-faced card.
    totals = _tally(
        (
            item["data"].get("id") or item["data"].get("name", ""),
            item["data"],
            item["quantity"],
        )
        for item in processed_cards
    )
    for card, qty in totals.values():
        if qty > 1 and not _allows_duplicates(card):
            issues += _singleton_issue(card.get("name", "Unknown card"), qty)

    issues.extend(_color_identity_issues(processed_cards, cmdrs))
    issues.extend(_legality_issues(processed_cards, fmt))

    return issues


def _color_identity_issues(processed_cards: list, cmdrs: list) -> list:
    """Lists the cards whose color identity the commander does not cover."""
    if not cmdrs:
        return []  # Without a commander there is no identity to check against.

    allowed = set()
    for item in cmdrs:
        allowed.update(card_color_identity(item["data"]))

    issues = []
    for item in processed_cards:
        identity = set(card_color_identity(item["data"]))
        outside = identity - allowed
        if outside:
            name = item["data"].get("name", "Unknown card")
            issues.append(
                f"“{name}” ({_format_identity(sorted(outside, key=WUBRG.index))}) "
                f"falls outside the commander's color identity "
                f"({_format_identity([c for c in WUBRG if c in allowed])})."
            )
    return issues


def _legality_issues(processed_cards: list, fmt: str = DEFAULT_FORMAT) -> list:
    """Lists the cards the given format does not allow.

    Reads the format's own key inside Scryfall's ``legalities``, which separates
    a card banned from an otherwise legal pool (Black Lotus) from one that was
    never printed in a legal product (un-sets, Alchemy rebalances, playtest
    cards). Anything else — including a missing field, as on cards cached
    before it was persisted — counts as legal: an absent field must never fail
    a deck.

    The two lists genuinely differ: Sol Ring is legal in Commander and banned
    in Duel Commander.
    """
    rules = FORMATS[fmt]
    issues = []
    for item in processed_cards:
        card = item["data"]
        legality = (card.get("legalities") or {}).get(rules.legality_key)
        if legality not in ("banned", "not_legal"):
            continue
        name = card.get("name", "Unknown card")
        if legality == "banned":
            issues.append(f"“{name}” is banned in {rules.label}.")
        else:
            issues.append(f"“{name}” is not legal in {rules.label}.")
    return issues
