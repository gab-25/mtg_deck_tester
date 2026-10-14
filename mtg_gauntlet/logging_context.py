"""Per-job logging context.

A background job (a deck import, a match) runs across several modules; rather
than threading its id through every call, it is stashed in a :class:`ContextVar`
for the duration of the job and :class:`JobFilter` stamps it onto every log
record. ``contextvars`` are isolated per thread, so concurrent background jobs
don't leak ids into each other.
"""

import contextlib
import logging
from contextvars import ContextVar

_job_var: ContextVar[str | None] = ContextVar("job", default=None)


@contextlib.contextmanager
def job_log_context(kind: str, job_id):
    """Binds ``"<kind> <id>"`` to every log record emitted inside the block."""
    token = _job_var.set(f"{kind} {job_id}")
    try:
        yield
    finally:
        _job_var.reset(token)


class JobFilter(logging.Filter):
    """Adds a ``job`` field (``"[match <id>] "`` or ``""``) to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        job = _job_var.get()
        record.job = f"[{job}] " if job else ""
        return True
