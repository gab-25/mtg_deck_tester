"""Test settings: force an in-memory SQLite database.

Keeps the test suite hermetic (no Postgres, independent of any ``.env`` or
``DATABASE_URL``). Selected via ``DJANGO_SETTINGS_MODULE`` in the pytest config.
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Run deck imports and matches inline (no background thread) so tests are
# deterministic.
RUN_JOBS_IN_BACKGROUND = False
