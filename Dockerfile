# Production image for MTG Gauntlet (Django + gunicorn + WhiteNoise).
FROM python:3.14-slim

# Install uv (fast Python package manager).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# - PYTHONUNBUFFERED:      stream logs straight to the container (no buffering)
# - PYTHONDONTWRITEBYTECODE: don't litter the image with .pyc files
# - UV_COMPILE_BYTECODE:   precompile installed deps for faster startup
# - DEBUG=0:               run with production settings (manifest static, etc.)
# - PATH:                  use the project venv's executables directly
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DEBUG=0 \
    DJANGO_SETTINGS_MODULE=mtg_gauntlet.settings \
    WEB_CONCURRENCY=3 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Unprivileged user to run the app.
RUN useradd --create-home --uid 1000 appuser

# Install dependencies first for better layer caching. The project is not a
# package (see [tool.uv] package = false), so uv installs only the dependencies
# and this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application source.
COPY mtg_gauntlet ./mtg_gauntlet
COPY decks ./decks
COPY playtest ./playtest
COPY theme ./theme
COPY manage.py docker-entrypoint.sh ./

# Collect all static files for WhiteNoise to serve.
RUN python manage.py collectstatic --noinput \
    && chmod +x docker-entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "mtg_gauntlet.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
