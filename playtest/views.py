"""Match views: set up a match between agents, follow it, read its log."""

import logging

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from decks.formats import playable_formats
from decks.models import Deck, MetagameDeck
from mtg_gauntlet.access import get_owned_or_404
from mtg_gauntlet.jobs import start_job

from . import forge
from .agents import typesafe
from .forms import DEFAULT_MATCH_FORMAT, MAX_OPPONENTS, METAGAME_PREFIX, SEAT_AGENT, parse_playtest_form
from .models import Match, Playtest, Seat
from .runner import run_match, start_game
from .summary import event_row, fallback_executions, fallback_reason

logger = logging.getLogger(__name__)


def _grid_context(user) -> dict:
    """Context for the playtest list, which polls itself while a match runs."""
    playtests = list(
        Playtest.objects.filter(owner=user).prefetch_related("matches__seats", "matches__winner")
    )
    sessions = [session_view(playtest) for playtest in playtests]
    return {"sessions": sessions, "has_busy": any(s["busy"] for s in sessions)}


def session_view(playtest) -> dict:
    """A session with each match, its opponent and the tally of results."""
    rows = []
    for match in sorted(playtest.matches.all(), key=lambda m: m.seed):
        seats = {seat.position: seat for seat in match.seats.all()}
        rows.append({"match": match, "opponent": seats.get(1), "result": match.result})
    tally = {}
    for row in rows:
        tally[row["result"]] = tally.get(row["result"], 0) + 1
    return {
        "playtest": playtest,
        "rows": rows,
        "tally": [(result, tally[result]) for result in RESULT_ORDER if result in tally],
        "busy": any(row["match"].is_busy for row in rows),
    }


# How a session's tally lists results.
RESULT_ORDER = ("won", "lost", "draw", "running", "pending", "stopped", "failed")


@login_required
@require_http_methods(["GET"])
def match_list(request):
    context = _grid_context(request.user)
    template = "playtest/partials/match_grid.html" if request.htmx else "playtest/match_list.html"
    return render(request, template, context)


def _form_context(user, post=None, **extra) -> dict:
    post = post or {}
    chosen = set(post.getlist("opponents")) if hasattr(post, "getlist") else set(post.get("opponents", []))
    return {
        "decks": Deck.objects.filter(owner=user, status=Deck.Status.READY),
        "formats": playable_formats(),
        "form_format": post.get("format", DEFAULT_MATCH_FORMAT),
        "form_deck": post.get("deck", ""),
        "form_opponents": chosen,
        "form_seed": post.get("seed", ""),
        "max_opponents": MAX_OPPONENTS,
        "metagame_decks": [
            (f"{METAGAME_PREFIX}{meta.id}", meta)
            for meta in MetagameDeck.objects.filter(status=MetagameDeck.Status.READY).order_by("format", "rank")
        ],
        "jev_model": typesafe.MODEL,
        "jev_available": typesafe.api_key() is not None,
        **extra,
    }


@login_required
@require_http_methods(["GET"])
def new_match(request):
    # "Test this deck" on a deck page pre-fills the deck to test.
    prefill = {"format": request.GET.get("format", DEFAULT_MATCH_FORMAT), "deck": request.GET.get("deck", "")}
    return render(request, "playtest/match_new.html", _form_context(request.user, prefill))


def _new_session(user, form) -> Playtest:
    """The session and one pending match per opponent: the deck under test in seat 0."""
    with transaction.atomic():
        playtest = Playtest.objects.create(
            owner=user, format=form.format, deck=form.deck, deck_name=form.deck.name, seed=form.seed
        )
        for index, opponent in enumerate(form.opponents):
            match = Match.objects.create(owner=user, playtest=playtest, format=form.format, seed=form.seed + index)
            Seat.objects.bulk_create([
                Seat(match=match, position=0, deck=form.deck, deck_name=form.deck.name, agent=SEAT_AGENT),
                Seat(match=match, position=1, deck=opponent.deck, metagame_deck=opponent.metagame_deck,
                     deck_name=opponent.name, agent=SEAT_AGENT),
            ])
    return playtest


@login_required
@require_http_methods(["POST"])
def create_match(request):
    """Starts a test session: the deck plays one match against each opponent."""
    form, errors = parse_playtest_form(request.POST, request.user)
    if errors:
        context = _form_context(request.user, request.POST, errors=errors)
        return render(request, "playtest/match_new.html", context, status=422)

    playtest = _new_session(request.user, form)
    matches = list(playtest.matches.order_by("seed"))
    # Forge checks the decks now: a deck it cannot play never becomes a session.
    errors, status = [], 422
    for match in matches:
        try:
            start_game(match)
        except forge.ForgeDeckError as exc:
            errors += [f"{name}: Forge does not know {', '.join(cards)}." for name, cards in exc.unresolved.items()]
        except Exception as exc:  # noqa: BLE001 - a bridge error, or an answer we cannot read
            if not isinstance(exc, forge.ForgeError):
                logger.exception("Could not start the game")
            errors.append(f"The playtest could not start: {exc}")
            status = 503
            break
    if errors:
        for match in matches:
            match.refresh_from_db()
            _abort(match)
        playtest.delete()
        context = _form_context(request.user, request.POST, errors=list(dict.fromkeys(errors)))
        return render(request, "playtest/match_new.html", context, status=status)
    for match in matches:
        start_job(run_match, match.id)
    return redirect("playtest_detail", playtest_id=playtest.id)


def _playtest_context(playtest) -> dict:
    return {"session": session_view(playtest), "playtest": playtest}


@login_required
@require_http_methods(["GET"])
def playtest_detail(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    return render(request, "playtest/playtest_detail.html", _playtest_context(playtest))


@login_required
@require_http_methods(["GET"])
def playtest_live(request, playtest_id):
    """The session's results, polled by HTMX while a match runs."""
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    return render(request, "playtest/partials/playtest_live.html", _playtest_context(playtest))


@login_required
@require_http_methods(["POST"])
def stop_playtest(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    for match in playtest.matches.all():
        _stop(match)
    return redirect("playtest_detail", playtest_id=playtest.id)


@login_required
@require_http_methods(["POST"])
def delete_playtest(request, playtest_id):
    playtest = get_owned_or_404(Playtest, request.user, playtest_id)
    for match in playtest.matches.all():
        if match.is_busy:
            _abort(match)
    playtest.delete()
    return redirect("match_list")


# Forge's log categories left out of the match page (still stored): every
# step of every turn, and each mana ability.
QUIET_LOG = {"PHASE", "MANA"}


def log_rows(events, names: dict[int, str]) -> list[dict]:
    """The match log as the page shows it: Forge's narrative, the agents'
    decisions with their confidence, fallbacks with their reason, and runs of
    Forge-AI decisions folded into one line."""
    rows = []
    # A fallback's forge_ai twin is the same decision: the fallback line says it.
    executions = fallback_executions([event_row(event) for event in events])
    for index, event in enumerate(events):
        payload = event.payload or {}
        if index in executions or (event.kind == "log" and payload.get("type") in QUIET_LOG):
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
        "jev_model": typesafe.MODEL,
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
    if not match.forge_game_id:
        return
    try:
        forge.client().abort(match.forge_game_id)
    except forge.ForgeError as exc:
        logger.warning("Could not abort game %s: %s", match.forge_game_id, exc)


def _stop(match) -> None:
    """Stops a running match: the bridge aborts the game and the runner records it."""
    if not match.is_busy:
        return
    _abort(match)
    # The runner records the end when it follows the game; if none does any
    # more (the process restarted), this keeps the match from polling forever.
    Match.objects.filter(pk=match.pk, status__in=[Match.Status.PENDING, Match.Status.RUNNING]).update(
        status=Match.Status.STOPPED, end_reason="aborted", finished_at=timezone.now()
    )


@login_required
@require_http_methods(["POST"])
def stop_match(request, match_id):
    match = get_owned_or_404(Match, request.user, match_id)
    _stop(match)
    return redirect("match_detail", match_id=match.id)


@login_required
@require_http_methods(["POST"])
def delete_match(request, match_id):
    match = get_owned_or_404(Match, request.user, match_id)
    if match.is_busy:
        _abort(match)
    playtest_id = match.playtest_id
    match.delete()
    return redirect("playtest_detail", playtest_id=playtest_id)
