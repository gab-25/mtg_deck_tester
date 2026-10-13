# CLAUDE.md

Django app where AI agents playtest competitive Magic: The Gathering decks (Standard,
Pioneer, Modern, Legacy, Vintage, Pauper, Duel Commander; Commander decks import but
two-seat play only) under real rules: Forge, behind the
[`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine) bridge, plays every
game, and Jev plays every seat, picking among its options (users pick decks, not
agents). Start
from `README.md` (features, rules, configuration); details live in `docs/`.

## Commands

```bash
uv sync                                    # install deps (dev group included)
uv run pytest                              # full suite, hermetic, ~seconds
FORGE_URL=http://localhost:8080 uv run pytest -m forge  # opt-in: against a real bridge
uv run python -m mtg_deck_tester           # migrate + runserver (HOST/PORT/RELOAD)
uv run python manage.py makemigrations     # after model/choices changes
uv run python manage.py load_oracle_cards  # fill the card cache from Scryfall bulk data
uv run python manage.py sync_metagame      # import the top decks of each format (network)
docker compose up --build                  # Postgres + the Forge bridge + gunicorn on :8000
docker compose up -d db                    # just Postgres, for local runs
docker compose up -d forge                 # just the Forge bridge, on localhost:8080
```

The default `DATABASE_URL` is the Compose Postgres on localhost; set
`DATABASE_URL=sqlite:///./mtg.db` to run without it. `FORGE_URL` defaults to
`http://localhost:8080`. Tests always use in-memory SQLite
(`mtg_deck_tester/settings_test.py`).

## Layout

- `mtg_deck_tester/` — settings, URLs, layout templates, background jobs, ownership.
- `decks/` — decklist parsing, Scryfall fetch + cache, legality, format registry →
  `docs/deck-import.md`.
- `playtest/forge.py` — the HTTP client of the Forge bridge → `docs/forge.md`.
- `playtest/views.py`, `playtest/forms.py` — a playtest session: the deck to test
  plays one match against each chosen opponent.
- `playtest/runner.py` — starts a match's game on the bridge, copies its events into
  `MatchEvent` rows, stores the result and the summary (`playtest/summary.py`).
- `playtest/workers.py` — the worker pool answering the bridge's decisions with each
  seat's agent → `docs/forge.md`.
- `playtest/agents/` — the Jev agent and its client →
  `docs/agents.md`.
- `theme/static/` — precompiled stylesheet, vendored fonts and icons.
- Overall flow (request → background thread → stored result, HTMX polling) →
  `docs/architecture.md`; tests, Docker, migrations → `docs/development.md`.

## Conventions

- Code, comments, docstrings, commit messages, README, `docs/` and issues are in
  English.
- Forge is the rules engine: never re-implement rules or card effects here. An agent
  only picks one of the bridge's options (by index) or delegates to Forge's AI; no
  model is ever the rules engine.
- `decks/formats.py` is the single source for format parameters (its `forge_format`
  is the bridge's name). Adding a format, a `Seat.Agent` or any status/choice value
  needs a migration.
- Templates may only use CSS classes present in `theme/static/css/styles.css` and
  icon names the vendored Material Symbols subset can render (no `j`/`q`), each icon
  with `translate="no"`. There is no CSS build: `tests/test_assets.py` enforces this;
  do not grow `KNOWN_UNCOMPILED`.
- Tests must never call Scryfall, OpenRouter or the Forge bridge (`tests/conftest.py`
  fails any that does): use the `fake_forge` fixture (`tests/fakeforge.py`),
  monkeypatch `fetch_card_data` / `fetch_card_raw`, `requests` or
  `typesafe.system_one`, or pass a fake `ask`. Only tests marked `forge`
  reach a real bridge. Shared builders are in `tests/factories.py`.
- Every deck/match view loads rows through `mtg_deck_tester.access.get_owned_or_404`.

## Workflow

- One branch per issue (`feature/<name>`, `bugfix/<name>`), PR to `main`.
- CI runs only on pushes to `main`: run `uv run pytest` before opening a PR.
- `docs/superpowers/` (working specs and plans) is gitignored: never commit it.
