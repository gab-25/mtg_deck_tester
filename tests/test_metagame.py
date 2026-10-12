"""Tests for metagame decks: the model and the sync (no network)."""

import pytest

from decks.models import MetagameDeck

from .factories import constructed_cards


def _meta(**fields):
    defaults = {
        "format": "modern",
        "source": MetagameDeck.Source.MTGTOP8,
        "archetype": "UR Aggro",
        "source_url": "https://www.mtgtop8.com/event?e=1&d=2&f=MO",
        "meta_share": 8.4,
        "rank": 1,
        "raw_decklist": "",
        "cards": constructed_cards(),
        "color_identity": ["R"],
        "coverage": {"percent": 46},
    }
    defaults.update(fields)
    return MetagameDeck.objects.create(**defaults)


@pytest.mark.django_db
def test_a_metagame_deck_counts_its_main_deck_and_flags_low_coverage():
    deck = _meta()
    assert deck.total_cards == 60 and not deck.has_commander
    assert deck.low_coverage
    assert deck.label == "UR Aggro — 8.4% — coverage 46%"
    assert not _meta(coverage={"percent": 80}).low_coverage
    assert _meta(meta_share=None, coverage={}).label == "UR Aggro"
