# Development

The project runs on Python 3.14 and uses [`uv`](https://docs.astral.sh/uv/) for
dependencies (`pyproject.toml`, `uv.lock`). It is an application, not a package
(`[tool.uv] package = false`): uv installs the dependencies and the code runs from
the working directory.

## Running locally

```bash
uv sync                                   # dependencies, dev group included
cp .env.example .env                      # then edit it
docker compose up -d db                   # optional: just Postgres, on localhost:5432
uv run python manage.py createsuperuser
uv run python -m mtg_deck_tester          # migrate + runserver on HOST:PORT
```

`python -m mtg_deck_tester` (`mtg_deck_tester/__main__.py`) applies pending
migrations and starts Django's `runserver` on `HOST`/`PORT` (default `0.0.0.0:8000`),
with auto-reload only when `RELOAD` is truthy (`.env.example` sets `RELOAD=1`).

The default `DATABASE_URL` points at the Compose Postgres
(`postgresql://mtg:mtg@localhost:5432/mtg_tester`). For a database-free setup use
SQLite instead, e.g. `DATABASE_URL=sqlite:///./mtg.db` (`*.db` is gitignored).

Everything is configured through environment variables; a `.env` file is loaded by
`mtg_deck_tester/__init__.py` (real environment variables win). The full table is in
the README's *Configuration* section.

## Running with Docker

```bash
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

- `db`: `postgres:16-alpine`, data in the `pgdata` volume, published on 5432.
- `web`: the `Dockerfile` image — `uv sync --frozen --no-dev`, `collectstatic` at
  build time, `DEBUG=0`, gunicorn on port 8000 as an unprivileged user.
  `docker-entrypoint.sh` runs `migrate` before starting the server.
- `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` are passed through from your shell.

CI (`.github/workflows/ci.yml`) runs on pushes to `main` only: `uv sync --frozen`,
`uv run pytest`, then builds and pushes a multi-arch image to
`ghcr.io/<owner>/<repo>` (`latest` and `sha-…` tags). Pull requests are not tested
by CI — run the suite locally before opening one.

## Tests

```bash
uv run pytest                     # whole suite, a few seconds
uv run pytest tests/test_engine.py -k commander
```

The suite is **hermetic**:

- `pytest.ini_options` selects `mtg_deck_tester.settings_test`: in-memory SQLite,
  independent of `.env` and `DATABASE_URL`.
- `RUN_JOBS_IN_BACKGROUND = False`: imports and matches run inline, so a test can
  POST a form and assert on the finished row.
- **No network.** Tests never reach Scryfall or OpenRouter. They monkeypatch
  `decks.importer.fetch_card_data`, `requests.get` / `requests.post` on the module
  under test, or `openrouter.chat`, or pass a fake `chat` to `LLMAgent`. A new test
  that would hit the network is a bug.
- Shared fixtures are plain functions in `tests/factories.py`: engine decks
  (`engine_deck`, `bear`, `FOREST`), processed Scryfall cards (`scryfall_card`,
  `stored_cards`), `make_deck(owner, …)` and `legal_decklist()`.
- `Deck.status` defaults to `ready`, so decks created directly in tests need no
  import.

| File | Covers |
|---|---|
| `test_decklist.py`, `test_cards.py`, `test_commander.py` | Parsing, card classification, legality |
| `test_scryfall.py`, `test_cache.py`, `test_load_oracle_cards.py` | Fetching, cache backends, bulk load |
| `test_engine.py`, `test_agents.py`, `test_runner.py` | Engine, agents and OpenRouter client, runner |
| `test_decks_web.py`, `test_matches_web.py` | Views end to end through the Django test client |
| `test_migrations.py` | The `0002_card_image_urls` data migration |
| `test_assets.py` | Template classes and icons vs. the vendored assets (below) |

## Templates, styles and icons

The stylesheet and icon font are **prebuilt artifacts committed to the repo**: there
is no Tailwind build and no font subsetting step here. `tests/test_assets.py` guards
what that implies:

- **Classes**: every `class="…"` token in a template must exist in
  `theme/static/css/styles.css`. A class that is not compiled in does nothing.
  `KNOWN_UNCOMPILED` lists the legacy exceptions; do not add to it — use classes the
  stylesheet already has, or plain CSS in the stylesheet.
- **Icons**: icons are Material Symbols ligatures (`<span class="ms" …>name</span>`)
  from `theme/static/vendor/gfonts/woff2/materialsymbols-subset.woff2`. The subset
  lacks the glyphs `j` and `q`, so names such as `query_stats` render as text. Check
  a new icon in the browser too.
- **Translation**: every icon span needs `translate="no"`, or a browser page
  translator rewrites the ligature name. `theme/static/js/untranslated-title.js` does
  the same job for the tab title.

Fonts are vendored under `theme/static/vendor/` (no CDN). Card images are the only
remote assets, loaded from `cards.scryfall.io`.

## Migrations

```bash
uv run python manage.py makemigrations
uv run python manage.py makemigrations --check --dry-run   # nothing pending?
uv run python manage.py migrate
```

Without a reachable Postgres, `makemigrations` still works but warns that it could
not run its consistency check; prefix it with `DATABASE_URL=sqlite:///:memory:` to
silence that.

Things that produce a migration without looking like a schema change:

- adding a format to `decks/formats.py` (`Deck.format` and `Match.format` choices —
  see [Deck import](deck-import.md#adding-a-format));
- adding a `Seat.Agent`, a `Deck.Status`, a `Match.Status` or `Match.EndReason`
  value.

Changing the shape of the stored card data (`Deck.cards[*]["data"]`) needs a data
migration for decks already imported, like `decks/migrations/0002_card_image_urls.py`,
with a test in `tests/test_migrations.py`.

## Logging

Console logging is set up in `settings.LOGGING` for the `mtg_deck_tester`, `decks`
and `playtest` loggers at `LOG_LEVEL` (default `INFO`). Lines from a background job
carry a `[deck <id>]` / `[match <id>]` prefix. Use `logging.getLogger(__name__)` in
new modules of these packages.
