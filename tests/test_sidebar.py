"""The sidebar every page shows: counts and the engine's local state."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from mtg_gauntlet.context_processors import sidebar
from playtest.models import Match

from .factories import make_deck, make_match


@pytest.mark.django_db
def test_sidebar_counts_my_decks_playtests_and_running_matches(django_user_model, monkeypatch):
    monkeypatch.setenv("FORGE_WORKERS", "4")
    monkeypatch.setenv("FORGE_URL", "http://forge:8080")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    user = django_user_model.objects.create_user(username="u")
    make_deck(user)
    make_match(user, status=Match.Status.RUNNING)
    make_match(django_user_model.objects.create_user(username="other"), status=Match.Status.RUNNING)
    request = RequestFactory().get("/")
    request.user = user
    data = sidebar(request)["sidebar"]
    assert (data["deck_count"], data["playtest_count"], data["running"], data["workers"]) == (1, 1, 1, 4)
    assert data["jev_ready"] is False and data["forge_url"] == "http://forge:8080"


def test_sidebar_is_empty_for_anonymous_users():
    request = RequestFactory().get("/login")
    request.user = AnonymousUser()
    assert sidebar(request) == {"sidebar": {}}
