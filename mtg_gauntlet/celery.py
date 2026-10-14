"""The Celery app: playtest matches run on its worker (see docs/architecture.md).

Configured from Django settings (``CELERY_*``); tasks live in each app's
``tasks.py``.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mtg_gauntlet.settings")

app = Celery("mtg_gauntlet")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
