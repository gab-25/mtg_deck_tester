"""Background jobs: deck imports and matches run off the request thread."""

import threading

from django.conf import settings
from django.db import connection


def _run_and_release(target, args):
    """Thread entry point: runs the job, then releases its DB connection.

    The background thread gets its own connection from Django's thread-local
    pool; close it on the way out so it isn't left dangling.
    """
    try:
        target(*args)
    finally:
        connection.close()


def start_job(target, *args) -> None:
    """Runs ``target(*args)`` in a daemon thread, or inline when disabled (tests)."""
    if getattr(settings, "RUN_JOBS_IN_BACKGROUND", True):
        threading.Thread(target=_run_and_release, args=(target, args), daemon=True).start()
    else:
        target(*args)
