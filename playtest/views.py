"""Match views: set up a match between agents, follow it, read its log."""

import logging

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from decks.formats import playable_formats
from decks.models import Deck, MetagameDeck
from mtg_deck_tester.access import get_owned_or_404
from mtg_deck_tester.jobs import start_job

from . import forge
from .agents import openrouter, typesafe
from .forms import DEFAULT_MATCH_FORMAT, METAGAME_PREFIX, SEAT_ROWS, agent_choices, parse_match_form
from .models import Match, Seat
from .runner import run_match, start_game
from .summary import fallback_reason, table_names

logger = logging.getLogger(__name__)


def _grid_context(user) -> dict:
    """Context for the match grid, which polls itself while a match runs."""
    matches = list(
        Match.objects.filter(owner=user).select_related("winner").prefetch_related("seats")
    )
    return {"matches": matches, "has_busy": any(m.is_busy for m in matches)}


@login_required
@require_http_methods(["GET"])
def match_list(request):
    context = _grid_context(request.user)
    template = "playtest/partials/match_grid.html" if request.htmx else "playtest/match_list.html"
    return render(request, template, context)


def _form_context(user, post=None, **extra) -> dict:
    post = post or {}
    decks = Deck.objects.filter(owner=user, status=Deck.Status.READY)
    rows = [
        {
            "index": i,
            "deck": post.get(f"seat-{i}-deck", ""),
            "agent": post.get(f"seat-{i}-agent", Seat.Agent.FORGE_AI),
            "model": post.get(f"seat-{i}-model", ""),
        }
        for i in range(SEAT_ROWS)
    ]
    return {
        "decks": decks,
        "rows": rows,
        "formats": playable_formats(),
        "form_format": post.get("format", DEFAULT_MATCH_FORMAT),
        "form_seed": post.get("seed", ""),
        "agent_choices": agent_choices(),
        "metagame_decks": [
            (f"{METAGAME_PREFIX}{meta.id}", meta)
            for meta in MetagameDeck.objects.filter(status=MetagameDeck.Status.READY).order_by("format", "rank")
        ],
        "default_model": openrouter.default_model(),
        "jev_model": typesafe.default_model(),
        "llm_available": openrouter.api_key() is not None,
        **extra,
    }


@login_required
@require_http_methods(["GET"])
def new_match(request):
    # "Play a match" on a deck page pre-fills the first seat with that deck.
    prefill = {"format": request.GET.get("format", DEFAULT_MATCH_FORMAT)}
    if request.GET.get("deck"):
        prefill["seat-0-deck"] = request.GET["deck"]
    return render(request, "playtest/match_new.html", _form_context(request.user, prefill))


@login_required
@require_http_methods(["POST"])
def create_match(request):
    form, errors = parse_match_form(request.POST, request.user)
    if errors:
        context = _form_context(request.user, request.POST, errors=errors)
        return render(request, "playtest/match_new.html", context, status=422)

    with transaction.atomic():
        match = Match.objects.create(owner=request.user, format=form.format, seed=form.seed)
        Seat.objects.bulk_create(
            Seat(
                match=match,
                position=position,
                deck=choice.deck,
                metagame_deck=choice.metagame_deck,
                deck_name=choice.name,
                agent=choice.agent,
                model=choice.model,
            )
            for position, choice in enumerate(form.seats)
        )
    # Forge checks the decks now: a deck it cannot play never becomes a match.
    try:
        start_game(match)
    except forge.ForgeDeckError as exc:
        seats = {seat_name: position for position, seat_name in enumerate(_seat_names(match))}
        match.delete()
        errors = [
            f"Seat {seats.get(name, 0) + 1} ({name}): Forge does not know {', '.join(cards)}."
            for name, cards in exc.unresolved.items()
        ]
        return render(request, "playtest/match_new.html", _form_context(request.user, request.POST, errors=errors), status=422)
    except forge.ForgeError as exc:
        match.delete()
        errors = [f"The match could not start: {exc}"]
        return render(request, "playtest/match_new.html", _form_context(request.user, request.POST, errors=errors), status=503)
    start_job(run_match, match.id)
    return redirect("match_detail", match_id=match.id)


def _seat_names(match) -> list[str]:
    return table_names(list(match.seats.all()))


# Forge's log categories left out of the match page (still stored): every
# step of every turn, and each mana ability.
QUIET_LOG = {"PHASE", "MANA"}


def log_rows(events, names: dict[int, str]) -> list[dict]:
    """The match log as the page shows it: Forge's narrative, the agents'
    decisions with their confidence, fallbacks with their reason, and runs of
    Forge-AI decisions folded into one line."""
    rows = []
    for event in events:
        payload = event.payload or {}
        if event.kind == "log" and payload.get("type") in QUIET_LOG:
            continue
        if event.kind == "forge_ai":
            last = rows[-1] if rows else None
            if last and last["kind"] == "forge_ai" and last["seat"] == event.seat_position:
                last["count"] += 1
                method = payload.get("method", "")
                if method and method not in last["methods"]:
                    last["methods"].append(method)
                continue
            rows.append({
                "kind": "forge_ai", "seq": event.seq, "seat": event.seat_position, "who": names.get(event.seat_position, ""),
                "count": 1, "methods": [payload.get("method", "")], "event": event,
            })
            continue
        row = {"kind": event.kind, "seq": event.seq, "seat": event.seat_position, "who": names.get(event.seat_position, ""), "event": event}
        if event.kind == "log":
            row["headline"] = payload.get("type") in ("TURN", "GAME_OUTCOME")
        elif event.kind == "fallback":
            row["reason"] = fallback_reason(payload)
        rows.append(row)
    return rows


def _live_context(match) -> dict:
    seats = list(match.seats.all())
    summary = match.summary or {}
    by_seat = {s["seat"]: s for s in summary.get("seats", [])}
    events = list(match.events.all())
    return {
        "match": match,
        "seats": seats,
        "event_count": len(events),
        "log": log_rows(events, {seat.position: seat.deck_name for seat in seats}),
        "summary": summary,
        "seat_stats": [(seat, by_seat.get(seat.position)) for seat in seats],
        "default_model": openrouter.default_model(),
        "jev_model": typesafe.default_model(),
    }


@login_required
@require_http_methods(["GET"])
def match_detail(request, match_id):
    match = get_owned_or_404(Match, request.user, match_id)
    return render(request, "playtest/match_detail.html", _live_context(match))


@login_required
@require_http_methods(["GET"])
def match_live(request, match_id):
    """The scoreboard and log region, polled by HTMX while the match runs."""
    match = get_owned_or_404(Match, request.user, match_id)
    return render(request, "playtest/partials/match_live.html", _live_context(match))


def _abort(match) -> None:
    """Ends the match's game on the bridge; a bridge that is gone has nothing to end."""
    if not match.forge_game_id or not match.is_busy:
        return
    try:
        forge.client().abort(match.forge_game_id)
    except forge.ForgeError as exc:
        logger.warning("Could not abort game %s: %s", match.forge_game_id, exc)


@login_required
@require_http_methods(["POST"])
def stop_match(request, match_id):
    """Stops a running match: the bridge aborts the game and the runner records it."""
    match = get_owned_or_404(Match, request.user, match_id)
    _abort(match)
    return redirect("match_detail", match_id=match.id)


@login_required
@require_http_methods(["POST"])
def delete_match(request, match_id):
    match = get_owned_or_404(Match, request.user, match_id)
    _abort(match)
    match.delete()
    return redirect("match_list")
