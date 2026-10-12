"""Tests for the format registry."""

import pytest

from decks.formats import CONSTRUCTED_FAMILY, CONSTRUCTED_FORMATS, FORMATS, format_choices


def test_commander_formats_are_unchanged():
    commander, duel = FORMATS["commander"], FORMATS["duel"]
    assert (commander.starting_life, commander.min_seats, commander.max_seats, commander.commander_damage_limit) == (40, 2, 4, 21)
    assert (duel.starting_life, duel.max_seats, duel.commander_damage_limit) == (20, 2, None)
    assert commander.has_commander and duel.has_commander
    assert (commander.deck_size, commander.exact_size, commander.max_copies) == (100, True, 1)


@pytest.mark.parametrize("key", ["standard", "pioneer", "modern", "legacy", "vintage", "pauper"])
def test_constructed_formats(key):
    fmt = FORMATS[key]
    assert fmt.family == CONSTRUCTED_FAMILY and not fmt.has_commander
    assert fmt.legality_key == key
    assert (fmt.starting_life, fmt.min_seats, fmt.max_seats, fmt.commander_damage_limit) == (20, 2, 2, None)
    assert (fmt.deck_size, fmt.exact_size, fmt.sideboard_size, fmt.max_copies) == (60, False, 15, 4)


def test_every_format_is_offered():
    assert [key for key, _ in format_choices()] == ["commander", "duel", *CONSTRUCTED_FORMATS]
