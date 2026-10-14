"""Which sidebar section a page belongs to, from its route name."""

from django import template

register = template.Library()

SECTIONS = (
    ("metagame", ("metagame",)),
    ("playtests", ("playtest", "match")),
    ("decks", ("deck",)),
    ("home", ("home",)),
)


@register.filter
def nav_section(url_name) -> str:
    """``home``, ``decks``, ``playtests``, ``metagame``, or ``""`` for a page outside them."""
    name = url_name or ""
    for section, words in SECTIONS:
        if any(word in name for word in words):
            return section
    return ""
