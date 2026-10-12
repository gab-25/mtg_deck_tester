# Architecture

MTG Deck Tester is one Django project (`mtg_deck_tester`) with three apps:

| App | What it owns |
|---|---|
| `mtg_deck_tester/` | Settings, root URLs, the layout templates and the home page, plus the plumbing every app shares: background jobs (`jobs.py`), the ownership check (`access.py`), per-job log stamping (`logging_context.py`). |
| `decks/` | Decks and the Scryfall card cache: decklist parsing, card fetching, legality, the format registry. See [Deck import](deck-import.md). |
| `playtest/` | Matches: the rules kernel, the card compiler (judge) and its cache, the agents that play, the match summary, and the runner that connects them to the database. See [Game engine](engine.md), [Card compilation](card-compilation.md) and [Agents](agents.md). |
| `theme/` | Static assets only: the precompiled stylesheet, vendored fonts, favicons. No Python beyond its `AppConfig`. |

## Request → background job → stored result

Both long-running operations — importing a deck and playing a match — follow the
same shape:

```
POST form ──► cheap validation ──► row created (status = pending) ──► start_job(...)
   │                                                                     │
   └─► redirect to a page that polls (HTMX) ◄── row updated as it runs ◄─┘
```

1. **The view validates what it can synchronously** and re-renders the form with
   status 422 on errors (`decks.importer.decklist_errors`,
   `playtest.forms.parse_match_form`).
2. **It creates the row** (`Deck` with `status=pending`; `Match` + its `Seat`s in one
   transaction) and calls `mtg_deck_tester.jobs.start_job(target, id)`.
3. **`start_job` runs the job in a daemon thread** (`RUN_JOBS_IN_BACKGROUND=1`, the
   default) or inline (tests set it to `False` in `settings_test.py`). The thread
   closes its own DB connection when done. There is no queue and no worker process:
   a job lives and dies with the web process that started it.
4. **The job writes its progress and outcome back to the row**:
   - `decks.importer.run_import` moves the deck `pending → processing → ready | failed`
     and stores `cards`, `commander`, `color_identity` (or `error`).
   - `playtest.runner.run_match` moves the match `pending → running → finished | failed`,
     first compiles every seat's main deck (through the `card_rules` cache) and
     stores each seat's coverage, then
     writes one `MatchEvent` per engine event as it happens, keeps `Match.round`
     current, and at the end stores the winner, the end reason, each seat's final
     life / elimination round and the deterministic `Match.summary`.
5. **The page polls**. The deck list and match list re-fetch their grid partial while
   anything is busy (`has_busy` in the context; the view returns the partial when
   `request.htmx` is set). The match page polls `matches/<id>/live`, which renders
   the scoreboard and the log.

Every log line emitted inside a job is prefixed with `[deck <id>]` or
`[match <id>]` (`job_log_context` + the `JobFilter` logging filter), so concurrent
jobs can be told apart on the console.

## Boundaries

- **The engine is pure Python.** `playtest/engine/` imports neither Django nor the
  agents at runtime (only a `TYPE_CHECKING` import of the `Agent` protocol). It
  takes `DeckSpec`s, agents and `GameRules`, and reports `Event`s through a callback.
  Everything it needs from the database is translated by `playtest/runner.py`:
  compiled cards → `CardSpec` (`card_spec`), compiled deck → `DeckSpec`
  (`deck_spec`), `decks.formats.Format` → `GameRules` (`game_rules`), `Seat` →
  agent (`build_agent`).
- **No model is the rules engine.** The kernel decides which actions are legal;
  the judge only picks among closed options for each clause, and an agent only
  picks one legal action. Every program is validated in code before use.
- **Agents only see what the engine hands them**: a per-seat view (`player_view`)
  and the list of legal actions. They return one of those actions; the engine
  rejects anything else.
- **`decks/rules/` is pure too**: parsing and legality are functions over plain
  dicts, no I/O. The importer is the one place that combines them with Scryfall and
  the cache.
- **Formats are data.** `decks/formats.py` holds the registry both apps read: deck
  legality uses `legality_key` and the family's construction rules, the match form
  uses the seat counts, the runner builds
  `GameRules` from `starting_life`, `commander_damage_limit` and the label.

## Data model

| Table | Model | Notes |
|---|---|---|
| `decks` | `decks.Deck` | UUID primary key. `cards` is a JSON list of `{"quantity", "is_commander", "board"?, "data"}` where `data` is the processed Scryfall card (see [Deck import](deck-import.md#stored-card-shape)) and `board` is `"side"` for a constructed sideboard card. |
| `scryfall_cards` | `decks.ScryfallCard` | Raw Scryfall JSON keyed by `card_en_<slug>`, shared by every user. `{"error": "not_found"}` caches a miss. |
| `scryfall_bulk_loads` | `decks.BulkDataLoad` | One row per `load_oracle_cards` run (for `--if-newer`). |
| `metagame_decks` | `decks.MetagameDeck` | The top decks of each format, replaced by every `sync_metagame`; shared and read-only, with source, archetype, meta share, rank, cards and coverage. |
| `card_rules` | `decks.CardRule` | Compiled card programs keyed by `oracle_id` + DSL version; opaque JSON to `decks`. |
| `matches` | `playtest.Match` | UUID primary key, `seed`, `max_rounds`, status, winner (a `Seat`), end reason, `summary` (JSON). |
| `seats` | `playtest.Seat` | Position, agent (`heuristic`/`jev`/`llm`/`random`), optional model id, the deck it plays — a user `deck` or a `metagame_deck` — and `coverage` (JSON). `deck` is `SET_NULL` on delete; `deck_name` keeps the name. |
| `match_events` | `playtest.MatchEvent` | The log: `seq`, `round`, `turn`, `step`, `seat_position`, `kind`, `text`, `payload` (Jev confidence and probabilities live here), `reasoning`. |

**Ownership.** `Deck` and `Match` have an `owner`. Every view loads them through
`mtg_deck_tester.access.get_owned_or_404`, which answers 404 (not 403) for someone
else's row. Login is required everywhere except the login page.

## Web layer

- Views are plain function views with `@login_required` and
  `@require_http_methods`.
- Templates: project-wide ones (layout, login, shared partials) live in
  `mtg_deck_tester/templates/`; each app keeps its own in
  `<app>/templates/<app>/`, with HTMX fragments under `partials/`.
- HTMX comes from `django-htmx` (`{% htmx_script %}` in `base.html`; the middleware
  sets `request.htmx`).
- Styling is a **precompiled** Tailwind stylesheet (`theme/static/css/styles.css`);
  there is no CSS build step in this repo. Icons are Material Symbols ligatures from
  a vendored font subset. See [Development](development.md#templates-styles-and-icons)
  for the constraints this puts on templates.
- Static files are served by WhiteNoise (hashed and compressed when `DEBUG` is off).
- Card images are never stored: `image_urls` point at `cards.scryfall.io`, and the
  zoom modal endpoint (`card-image`) only accepts URLs under that prefix.
