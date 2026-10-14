"""The home page: what is running, what needs attention, how the decks are doing."""

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from decks.formats import FORMATS
from decks.models import Deck, MetagameDeck
from decks.presentation import deck_pips
from playtest import forge
from playtest.models import Playtest
from playtest.stats import needs_attention, record
from playtest.views import session_view

RECENT = 5
# The sidebar asks the bridge's health on every page: a short timeout, and a
# cached answer, so a bridge that is down never slows the pages down.
FORGE_HEALTH_TIMEOUT = 2
FORGE_STATUS_TTL = 10
# The bridge's health state -> the sidebar's dot and label.
FORGE_STATES = {"online": "ok", "loading": "warn", "offline": "danger"}


def _playtests(user):
    return list(
        Playtest.objects.filter(owner=user).prefetch_related("matches__seats__metagame_deck", "matches__winner")
    )


def _running(playtests):
    """The most recent playtest still playing, as the running panel shows it."""
    for playtest in playtests:
        session = session_view(playtest)
        if session["busy"]:
            done = sum(row["result"] in ("won", "lost", "draw") for row in session["rows"])
            session["done"] = done
            session["total"] = len(session["rows"])
            session["percent"] = round(100 * done / session["total"]) if session["total"] else 0
            return session
    return None


@login_required
@require_http_methods(["GET"])
def home(request):
    playtests = _playtests(request.user)
    decks = list(Deck.objects.filter(owner=request.user))
    by_deck = {}
    for playtest in playtests:
        if playtest.deck_id:
            by_deck.setdefault(playtest.deck_id, []).append(playtest)
    rows = []
    for deck in decks:
        deck.pips = deck_pips(deck)
        tested = by_deck.get(deck.id, [])
        matches = [match for playtest in tested for match in playtest.matches.all()]
        rows.append({"deck": deck, "record": record(matches), "last": tested[0] if tested else None})
    synced = {
        row["format"]: row["synced"]
        for row in MetagameDeck.objects.values("format").annotate(synced=Max("fetched_at"))
    }
    recent = [
        {**session_view(playtest), "record": record(playtest.matches.all())} for playtest in playtests[:RECENT]
    ]
    return render(
        request,
        "home.html",
        {
            "running": _running(playtests),
            "deck_rows": rows,
            "notices": needs_attention(decks, synced, timezone.now()),
            "recent": recent,
            "formats": FORMATS.values(),
        },
    )


@login_required
@require_http_methods(["GET"])
def home_running(request):
    """The running panel, polled by HTMX while a playtest plays; empty once none does."""
    return render(request, "partials/running_now.html", {"running": _running(_playtests(request.user))})


def _forge_status() -> str:
    """"online", "loading" (Forge still reads its cards) or "offline"."""
    status = cache.get("forge_status")
    if status is None:
        try:
            health = forge.client().health(timeout=FORGE_HEALTH_TIMEOUT)
            status = "online" if health.get("status") == "ready" else "loading"
        except forge.ForgeError:
            status = "offline"
        cache.set("forge_status", status, FORGE_STATUS_TTL)
    return status


@login_required
@require_http_methods(["GET"])
def forge_status(request):
    """The sidebar's Forge bridge row, loaded apart from the page and polled by HTMX."""
    status = _forge_status()
    context = {"forge_url": forge.base_url(), "status": status, "dot": FORGE_STATES[status]}
    return render(request, "partials/forge_status.html", context)
