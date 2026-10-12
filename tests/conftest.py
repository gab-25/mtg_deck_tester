"""Suite-wide fixtures."""

import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The suite is hermetic: a request that reaches the transport fails the test.

    Tests stub ``requests.get`` / ``requests.post`` themselves; anything that
    slips past a stub lands here instead of on Scryfall or OpenRouter.
    """

    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"unexpected network call in a test: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", refuse)
