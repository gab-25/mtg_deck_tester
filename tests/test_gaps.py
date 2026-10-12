"""Tests for measuring what keeps cards out of the DSL."""

from playtest.engine.gaps import card_gaps, normalize

from .cards import basic_land, raw_card


def test_a_supported_card_has_no_gap():
    assert card_gaps(basic_land()) == []
    assert card_gaps(raw_card("Bolt", "Instant", "{R}", "Bolt deals 3 damage to any target.")) == []


def test_every_reason_is_listed_not_just_the_first():
    card = raw_card(
        "Mess",
        "Sorcery",
        "{2}",
        "Copy target spell.\n"
        "You may draw a card.\n"
        "Proliferate.",
    )
    gaps = card_gaps(card)
    assert "copies" in gaps
    assert "optional effect (may)" in gaps
    assert "no operation reads: proliferate" in gaps


def test_each_line_is_split_on_its_own():
    card = raw_card("Two", "Creature — Elf", "{1}{G}", "Whenever a creature you control dies, you gain 1 life.\nThis creature gets +1/+1.")
    assert card_gaps(card) == ["unsupported trigger: whenever a creature you control dies"]


def test_reasons_are_normalized_for_ranking():
    assert normalize("no operation reads: deals 3 damage to target creature with flying") == "no operation reads: deals N damage to target creature with flying"
