"""The home page: recent playtests and decks at a glance."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from decks.models import Deck
from decks.presentation import deck_pips
from playtest.agents import openrouter
from playtest.models import Playtest
from playtest.views import session_view

RECENT = 6


@login_required
@require_http_methods(["GET"])
def home(request):
    playtests = Playtest.objects.filter(owner=request.user).prefetch_related("matches__seats", "matches__winner")
    sessions = [session_view(playtest) for playtest in playtests[:RECENT]]
    decks = list(Deck.objects.filter(owner=request.user)[:RECENT])
    for deck in decks:
        deck.pips = deck_pips(deck)
    return render(
        request,
        "home.html",
        {
            "sessions": sessions,
            "decks": decks,
            "deck_count": Deck.objects.filter(owner=request.user).count(),
            "playtest_count": playtests.count(),
            "jev_ready": openrouter.api_key() is not None,
        },
    )
