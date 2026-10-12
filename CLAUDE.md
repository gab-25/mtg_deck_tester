# CLAUDE.md

Django app where AI agents playtest Magic: The Gathering Commander decks. Start from
`README.md` (features, rules, configuration); details live in `docs/`.

## Commands

```bash
uv sync                                    # install deps (dev group included)
uv run pytest                              # full suite, hermetic, ~seconds
uv run python -m mtg_deck_tester           # migrate + runserver (HOST/PORT/RELOAD)
uv run python manage.py makemigrations     # after model/choices changes
uv run python manage.py load_oracle_cards  # fill the card cache from Scryfall bulk data
docker compose up --build                  # Postgres + gunicorn image on :8000
docker compose up -d db                    # just Postgres, for local runs
```

The default `DATABASE_URL` is the Compose Postgres on localhost; set
`DATABASE_URL=sqlite:///./mtg.db` to run without it. Tests always use in-memory
SQLite (`mtg_deck_tester/settings_test.py`).

## Layout

- `mtg_deck_tester/` — settings, URLs, layout templates, background jobs, ownership.
- `decks/` — decklist parsing, Scryfall fetch + cache, legality, format registry →
  `docs/deck-import.md`.
- `playtest/engine/` — the game engine (pure Python) → `docs/engine.md`.
- `playtest/agents/` — random agent, LLM agent, OpenRouter client → `docs/agents.md`.
- `playtest/runner.py` — stored decks → engine → stored events; the only bridge
  between the engine and Django.
- `theme/static/` — precompiled stylesheet, vendored fonts and icons.
- Overall flow (request → background thread → stored result, HTMX polling) →
  `docs/architecture.md`; tests, Docker, migrations → `docs/development.md`.

## Conventions

- Code, comments, docstrings, commit messages, README, `docs/` and issues are in
  English.
- `playtest/engine/` never imports Django (or anything doing I/O). New rules go in
  the engine; agents and the runner should not need to change.
- The docstring of `playtest/engine/rules.py` is sent to the LLM as the rules
  summary: keep it, the README *Rules* section and `docs/engine.md` in sync.
- `decks/formats.py` is the single source for format parameters. Adding a format, a
  `Seat.Agent` or any status/choice value needs a migration.
- Templates may only use CSS classes present in `theme/static/css/styles.css` and
  icon names the vendored Material Symbols subset can render (no `j`/`q`), each icon
  with `translate="no"`. There is no CSS build: `tests/test_assets.py` enforces this;
  do not grow `KNOWN_UNCOMPILED`.
- Tests must never call Scryfall or OpenRouter: monkeypatch `fetch_card_data`,
  `requests`, `openrouter.chat`, or pass a fake `chat` to `LLMAgent`. Shared builders
  are in `tests/factories.py`.
- Every deck/match view loads rows through `mtg_deck_tester.access.get_owned_or_404`.

## Workflow

- One branch per issue (`feature/<name>`, `bugfix/<name>`), PR to `main`.
- CI runs only on pushes to `main`: run `uv run pytest` before opening a PR.
- `docs/superpowers/` (working specs and plans) is gitignored: never commit it.
