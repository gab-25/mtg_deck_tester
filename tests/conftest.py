"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _no_scryfall_in_matches(monkeypatch):
    """Matches read cards from the Scryfall cache; in tests it is empty, so never fetch.

    The runner then rebuilds each card from the deck's stored data.
    """
    monkeypatch.setattr("playtest.runner.fetch_card_raw", lambda name, cache: None)
