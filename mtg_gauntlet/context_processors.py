"""Template context processors shared across all views."""

from . import __version__


def app_version(request):
    """Exposes the project version (from pyproject.toml) to every template."""
    return {"app_version": __version__}


def sidebar(request):
    """What the sidebar shows on every page: counts and the engine's local state, no network."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"sidebar": {}}
    from decks.models import Deck
    from playtest import forge, workers
    from playtest.agents import typesafe
    from playtest.models import Match, Playtest

    return {
        "sidebar": {
            "deck_count": Deck.objects.filter(owner=user).count(),
            "playtest_count": Playtest.objects.filter(owner=user).count(),
            "jev_ready": typesafe.api_key() is not None,
            "running": Match.objects.filter(owner=user, status=Match.Status.RUNNING).count(),
            "workers": workers.worker_count(),
            "forge_url": forge.base_url(),
        }
    }
