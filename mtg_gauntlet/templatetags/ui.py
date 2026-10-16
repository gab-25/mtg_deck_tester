"""Small presentation filters shared by every page."""

from django import template

register = template.Library()

RESULT_LABELS = {
    "won": "Won",
    "lost": "Lost",
    "draw": "Draw",
    "running": "Playing",
    "pending": "Queued",
    "stopped": "Stopped",
    "failed": "Failed",
}


@register.filter
def result_label(result) -> str:
    """How a match result or status reads on the page: ``won`` → ``Won``, ``running`` → ``Playing``."""
    return RESULT_LABELS.get(result, str(result).capitalize())


@register.filter
def flag_label(check) -> str:
    """How a decision check (playtest.checks) reads on the page."""
    from playtest.checks import FLAG_LABELS

    return FLAG_LABELS.get(check, str(check).replace("_", " "))


@register.filter
def dash(value):
    """``—`` for a missing number, so an empty record never prints ``None``."""
    return "—" if value is None or value == "" else value
