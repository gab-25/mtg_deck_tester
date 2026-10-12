"""Match views: set up a match between agents, follow it, read its log."""

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from decks.formats import DEFAULT_FORMAT, FORMATS, format_choices
from decks.models import Deck, MetagameDeck
from mtg_deck_tester.access import get_owned_or_404
from mtg_deck_tester.jobs import start_job

from . import benchmarks
from .agents import openrouter, typesafe
from .forms import BENCHMARK_PREFIX, METAGAME_PREFIX, SEAT_ROWS, parse_match_form
from .models import Match, Seat
from .runner import run_match


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
            "agent": post.get(f"seat-{i}-agent", Seat.Agent.HEURISTIC),
            "model": post.get(f"seat-{i}-model", ""),
        }
        for i in range(SEAT_ROWS)
    ]
    return {
        "decks": decks,
        "rows": rows,
        "format_choices": format_choices(),
        "formats": FORMATS,
        "form_format": post.get("format", DEFAULT_FORMAT),
        "form_seed": post.get("seed", ""),
        "form_max_rounds": post.get("max_rounds", 20),
        "agent_choices": Seat.Agent.choices,
        "metagame_decks": [
            (f"{METAGAME_PREFIX}{meta.id}", meta)
            for meta in MetagameDeck.objects.filter(status=MetagameDeck.Status.READY).order_by("format", "rank")
        ],
        "benchmarks": [
            (BENCHMARK_PREFIX + key, label, benchmarks.family(key)) for key, label in benchmarks.choices()
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
    prefill = {"format": request.GET.get("format", DEFAULT_FORMAT)}
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
        match = Match.objects.create(
            owner=request.user,
            format=form.format,
            seed=form.seed,
            max_rounds=form.max_rounds,
        )
        Seat.objects.bulk_create(
            Seat(
                match=match,
                position=position,
                deck=choice.deck,
                benchmark=choice.benchmark,
                metagame_deck=choice.metagame_deck,
                deck_name=choice.name,
                agent=choice.agent,
                model=choice.model,
            )
            for position, choice in enumerate(form.seats)
        )
    start_job(run_match, match.id)
    return redirect("match_detail", match_id=match.id)


def _live_context(match) -> dict:
    seats = list(match.seats.all())
    summary = match.summary or {}
    by_seat = {s["seat"]: s for s in summary.get("seats", [])}
    return {
        "match": match,
        "seats": seats,
        "events": list(match.events.all()),
        "summary": summary,
        "summary_seats": [(seat, by_seat[seat.position]) for seat in seats if seat.position in by_seat],
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


@login_required
@require_http_methods(["POST"])
def delete_match(request, match_id):
    get_owned_or_404(Match, request.user, match_id).delete()
    return redirect("match_list")
