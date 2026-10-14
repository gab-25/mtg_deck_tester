"""Django settings for the MTG Gauntlet web service.

Configuration comes from environment variables (loaded from a ``.env`` file in
local development — see ``__init__.py``). The database connection string is the
``DATABASE_URL`` variable.
"""

import os
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = os.environ.get("DEBUG", "1").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = ["*"]

# When deployed behind a reverse proxy TLS is terminated at the ingress and the
# app receives plain HTTP, so Django would otherwise treat the request as
# insecure. Trust the forwarded scheme header to detect HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Origins Django accepts for CSRF-protected POSTs (must include the scheme).
# Comma-separated list, e.g. "https://tester.example.com".
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "theme",
    "django_htmx",
    "decks",
    "playtest",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves the collected static files directly from the app server
    # (no separate web server needed); must sit right after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Populates ``request.htmx`` from the HX-* request headers.
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "mtg_gauntlet.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-wide templates (layout, login, shared partials); each app
        # keeps its own under <app>/templates/<app>/.
        "DIRS": [BASE_DIR / "mtg_gauntlet" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "mtg_gauntlet.context_processors.app_version",
                "mtg_gauntlet.context_processors.sidebar",
            ],
            # The project package is not an app, so its tags load as builtins.
            "builtins": ["mtg_gauntlet.templatetags.nav"],
        },
    },
]

WSGI_APPLICATION = "mtg_gauntlet.wsgi.application"
ASGI_APPLICATION = "mtg_gauntlet.asgi.application"

DEFAULT_DATABASE_URL = "postgresql://mtg:mtg@localhost:5432/mtg_tester"


def _database_config(url: str) -> dict:
    """Builds a Django ``DATABASES['default']`` entry from a connection URL.

    Any ``+driver`` suffix on the scheme (e.g. ``postgresql+psycopg``) is
    stripped, since Django picks the driver from the backend itself.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.split("+", 1)[0]

    if scheme == "sqlite":
        # ``sqlite:///./mtg.db`` -> ``./mtg.db``; ``sqlite:///:memory:`` -> ``:memory:``.
        name = (parsed.netloc + parsed.path).lstrip("/")
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": name or ":memory:"}

    if scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": urllib.parse.unquote(parsed.username or ""),
            "PASSWORD": urllib.parse.unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
        }

    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


DATABASES = {
    "default": _database_config(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication: log-in gates the whole app; both views below are named routes.
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# In production (DEBUG off) WhiteNoise serves compressed, hash-versioned static
# files from STATIC_ROOT. In development the plain finder-based storage is used
# so the app works without running ``collectstatic`` first.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

USE_TZ = True
USE_I18N = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"

# Deck imports (minutes of Scryfall calls) and matches (possibly hundreds of
# agent calls) run in a background thread so the request returns immediately.
# Disabled in tests, where the work runs inline (see ``settings_test``).
RUN_JOBS_IN_BACKGROUND = os.environ.get("RUN_JOBS_IN_BACKGROUND", "1").lower() in {
    "1",
    "true",
    "yes",
}

# Surface application logs (including background job progress) on the console.
# Level is controlled by ``LOG_LEVEL`` (defaults to INFO).
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_APP_LOGGER = {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "job": {"()": "mtg_gauntlet.logging_context.JobFilter"},
    },
    "formatters": {
        "simple": {
            "format": "{asctime} {levelname} {name}: {job}{message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["job"],
        },
    },
    "loggers": {
        "mtg_gauntlet": _APP_LOGGER,
        "decks": _APP_LOGGER,
        "playtest": _APP_LOGGER,
    },
}
