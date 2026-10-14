"""Template context processors shared across all views."""

from . import __version__


def app_version(request):
    """Exposes the project version (from pyproject.toml) to every template."""
    return {"app_version": __version__}
