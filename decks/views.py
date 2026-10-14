"""Deck views: import a decklist, browse your decks, look at one."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from mtg_gauntlet.access import get_owned_or_404
from mtg_gauntlet.jobs import start_job

from .formats import DEFAULT_FORMAT, FORMATS, format_choices
from .importer import decklist_errors, run_import
from .models import Deck, MetagameDeck
from .presentation import card_groups, commander_cards, deck_pips, sideboard_group


def _grid_context(user, fmt="") -> dict:
    """Context for the deck list, which polls itself while an import runs."""
    from playtest.stats import record

    decks = list(Deck.objects.filter(owner=user).prefetch_related("playtests__matches__winner"))
    formats = [(key, FORMATS[key].label) for key in FORMATS if any(d.format == key for d in decks)]
    if fmt not in dict(formats):
        fmt = ""
    shown = [deck for deck in decks if not fmt or deck.format == fmt]
    for deck in shown:
        deck.pips = deck_pips(deck)
        deck.record = record(m for p in deck.playtests.all() for m in p.matches.all())
    return {
        "decks": shown,
        "formats": formats,
        "active_format": fmt,
        "has_busy": any(d.is_busy for d in decks),
    }


@login_required
@require_http_methods(["GET"])
def deck_list(request):
    context = _grid_context(request.user, request.GET.get("format", ""))
    # HTMX poll: return just the grid region so it can swap itself in place.
    template = "decks/partials/deck_grid.html" if request.htmx else "decks/deck_list.html"
    return render(request, template, context)


def _form_context(**extra) -> dict:
    return {"format_choices": format_choices(), "formats": FORMATS, "form_format": DEFAULT_FORMAT, **extra}


@login_required
@require_http_methods(["GET"])
def new_deck(request):
    return render(request, "decks/deck_new.html", _form_context())


@login_required
@require_http_methods(["POST"])
def create_deck(request):
    name = (request.POST.get("name") or "").strip() or "Untitled Deck"
    decklist = request.POST.get("decklist", "")
    fmt = request.POST.get("format")
    if fmt not in FORMATS:
        fmt = DEFAULT_FORMAT

    # Cheap validation first, so an illegal deck is reported inline; the
    # Scryfall fetch runs in the background afterwards.
    errors = decklist_errors(decklist, fmt)
    if errors:
        context = _form_context(
            errors=errors, form_name=name, form_decklist=decklist, form_format=fmt
        )
        return render(request, "decks/deck_new.html", context, status=422)

    deck = Deck.objects.create(
        owner=request.user,
        name=name,
        format=fmt,
        raw_decklist=decklist,
        status=Deck.Status.PENDING,
    )
    start_job(run_import, deck.id)
    # Post/Redirect/Get: the deck list shows the import and polls until it's done.
    return redirect("deck_list")


# The deck page's tabs, chosen with ``?tab=``.
DECK_TABS = ("results", "cards", "list")


@login_required
@require_http_methods(["GET"])
def deck_detail(request, deck_id):
    from playtest.stats import record, record_by_opponent

    deck = get_owned_or_404(Deck, request.user, deck_id)
    if deck.is_busy:
        return redirect("deck_list")
    tab = request.GET.get("tab")
    if tab not in DECK_TABS:
        tab = "results"
    playtests = list(deck.playtests.prefetch_related("matches__seats__metagame_deck", "matches__winner"))
    matches = [match for playtest in playtests for match in playtest.matches.all()]
    history = [
        {
            "playtest": playtest,
            "record": record(playtest.matches.all()),
            "busy": any(match.is_busy for match in playtest.matches.all()),
            "opponents": len(playtest.matches.all()),
        }
        for playtest in playtests
    ]
    return render(
        request,
        "decks/deck_detail.html",
        {
            "deck": deck,
            "tab": tab,
            "pips": deck_pips(deck),
            "record": record(matches),
            "opponents": record_by_opponent(matches),
            "history": history,
            "card_groups": card_groups(deck.cards),
            "commander_cards": commander_cards(deck.cards),
            "sideboard": sideboard_group(deck.cards),
        },
    )


@login_required
@require_http_methods(["POST"])
def deck_forge_check(request, deck_id):
    """What the Forge engine makes of the deck, on demand: cards it does not
    know (a match would be refused) and cards it cannot play fully. The
    answer is kept on the deck; a bridge that cannot answer changes nothing."""
    # The playtest app talks to Forge; imported here so decks keeps no
    # module-level dependency on it.
    from playtest import forge

    deck = get_owned_or_404(Deck, request.user, deck_id)
    context = {"deck": deck, "oob": True}
    try:
        check = forge.client().check_deck(
            deck.format, forge.decklist_text(deck.cards, has_commander=deck.has_commander)
        )
    except forge.ForgeError as exc:
        context["error"] = str(exc)
    else:
        deck.coverage = {
            "unresolved": check.get("unresolved") or [],
            "unsupported_cards": check.get("unsupported_cards") or [],
        }
        deck.forge_checked_at = timezone.now()
        deck.save(update_fields=["coverage", "forge_checked_at"])
        context["check"] = check
    return render(request, "decks/partials/forge_check.html", context)


@login_required
@require_http_methods(["POST"])
def delete_deck(request, deck_id):
    get_owned_or_404(Deck, request.user, deck_id).delete()
    return redirect("deck_list")


# Only card images on Scryfall's CDN may be shown in the zoom modal, so the
# endpoint can't be used to render arbitrary URLs.
SCRYFALL_IMAGE_PREFIX = "https://cards.scryfall.io/"


@login_required
@require_http_methods(["GET"])
def card_image_modal(request):
    """Returns the zoom-modal fragment for a card's faces (HTMX).

    Thumbnails ``hx-get`` this endpoint with one ``url`` per printed face, so a
    double-faced card ships both images at once and the flip costs no further
    request. Called without a ``url`` it returns an empty body, which the
    overlay uses to close itself.
    """
    urls = [url for url in request.GET.getlist("url") if url]
    if not urls:
        return HttpResponse("")
    if any(not url.startswith(SCRYFALL_IMAGE_PREFIX) for url in urls):
        return HttpResponse("Not a Scryfall image", status=400)
    return render(request, "partials/card_image_modal.html", {"image_urls": urls})


@login_required
@require_http_methods(["GET"])
def metagame(request):
    """Every format's metagame decks, as the last ``sync_metagame`` left them."""
    by_format = {key: [] for key in FORMATS}
    for deck in MetagameDeck.objects.all():
        by_format.setdefault(deck.format, []).append(deck)
    sections = [
        {
            "key": key,
            "label": FORMATS[key].label,
            "decks": decks,
            "synced": max((d.fetched_at for d in decks), default=None),
        }
        for key, decks in by_format.items()
        if key in FORMATS
    ]
    return render(request, "decks/metagame.html", {"sections": sections})
