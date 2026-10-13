# Architecture

MTG Deck Tester is one Django project (`mtg_deck_tester`) with three apps:

| App | What it owns |
|---|---|
| `mtg_deck_tester/` | Settings, root URLs, the layout templates and the home page, plus the plumbing every app shares: background jobs (`jobs.py`), the ownership check (`access.py`), per-job log stamping (`logging_context.py`). |
| `decks/` | Decks and the Scryfall card cache: decklist parsing, card fetching, legality, the format registry. See [Deck import](deck-import.md). |
| `playtest/` | Matches: the client of the Forge bridge (the rules engine), the runner and the worker pool that connect it to the database, the agents that play, and the match summary. See [Forge](forge.md) and [Agents](agents.md). |
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
   `playtest.forms.parse_playtest_form`).
2. **It creates the rows** (`Deck` with `status=pending`; for a playtest, the
   `Playtest` session and one `Match` per opponent with its two `Seat`s, in one
   transaction) and calls `mtg_deck_tester.jobs.start_job(target, id)` for each. A
   playtest first starts every match's game on the Forge bridge
   (`playtest.runner.start_game`): when Forge does not know a card (or the bridge is
   unreachable) the games already started are aborted, the session is deleted and the
   form is re-rendered naming the cards, so a refused deck never becomes a playtest.
3. **`start_job` runs the job in a daemon thread** (`RUN_JOBS_IN_BACKGROUND=1`, the
   default) or inline (tests set it to `False` in `settings_test.py`). The thread
   closes its own DB connection when done. There is no queue and no worker process:
   a job lives and dies with the web process that started it.
4. **The job writes its progress and outcome back to the row**:
   - `decks.importer.run_import` moves the deck `pending → processing → ready | failed`
     and stores `cards`, `commander`, `color_identity` (or `error`).
   - `playtest.runner.run_match` moves the match `pending → running → finished |
     stopped | failed`, copies one `MatchEvent` per bridge event as it arrives, keeps
     `Match.turn` current, and at the end stores the winner, Forge's end reason, each
     seat's final life and the deterministic `Match.summary`.
   - Meanwhile the **worker pool** (`playtest/workers.py`, `FORGE_WORKERS` threads
     started once per process with the first match) answers the bridge's decisions
     for every running match. Without a pool the runner answers its own.
5. **The page polls**. The deck list and playtest list re-fetch their grid partial while
   anything is busy (`has_busy` in the context; the view returns the partial when
   `request.htmx` is set). A playtest page polls `playtests/<id>/live` (one row per
   opponent with its result); a match page polls `matches/<id>/live`, which renders
   the scoreboard and the log.

Every log line emitted inside a job is prefixed with `[deck <id>]` or
`[match <id>]` (`job_log_context` + the `JobFilter` logging filter), so concurrent
jobs can be told apart on the console.

## Boundaries

- **Forge is the rules engine.** Rules and card effects live in Forge, behind the
  `mtg_forge_engine` bridge; this app never re-implements them.
  `playtest/forge.py` is the only module that speaks HTTP to the bridge.
- **No model is the rules engine.** Forge lists the legal options of each
  decision; an agent only picks one of them by index, or delegates to Forge's AI.
- **Agents only see what the bridge hands them**: the seat's filtered view, the
  decision kind, Forge's prompt and the numbered options with their facts.
- **`decks/rules/` is pure too**: parsing and legality are functions over plain
  dicts, no I/O. The importer is the one place that combines them with Scryfall and
  the cache.
- **Formats are data.** `decks/formats.py` holds the registry both apps read: deck
  legality uses `legality_key` and the family's construction rules, the match form
  uses the seat counts and `forge_format` (the format's name on the bridge, `None`
  for one it cannot seat), the summary reads `starting_life`.

## Data model

| Table | Model | Notes |
|---|---|---|
| `decks` | `decks.Deck` | UUID primary key. `cards` is a JSON list of `{"quantity", "is_commander", "board"?, "data"}` where `data` is the processed Scryfall card (see [Deck import](deck-import.md#stored-card-shape)) and `board` is `"side"` for a constructed sideboard card. |
| `scryfall_cards` | `decks.ScryfallCard` | Raw Scryfall JSON keyed by `card_en_<slug>`, shared by every user. `{"error": "not_found"}` caches a miss. |
| `scryfall_bulk_loads` | `decks.BulkDataLoad` | One row per `load_oracle_cards` run (for `--if-newer`). |
| `metagame_decks` | `decks.MetagameDeck` | The top decks of each format, replaced by every `sync_metagame`; shared and read-only, with source, archetype, meta share, rank, cards and `coverage` — Forge's check at sync (`{"unresolved", "unsupported_cards"}`, empty when the bridge was unreachable). |
| `playtests` | `playtest.Playtest` | A test session: UUID primary key, owner, format, the deck under test (`deck`, `SET_NULL`, with `deck_name`), `seed`. Its matches (one per opponent) are deleted with it. |
| `matches` | `playtest.Match` | UUID primary key, `playtest` (empty for matches from before sessions; seat 0 is the deck under test, seat 1 the opponent), `seed` (the session's plus the opponent's index), `forge_game_id`, `turn`, status (`pending`/`running`/`finished`/`failed`/`stopped`), winner (a `Seat`), `end_reason` (Forge's, verbatim), `started_at`, `summary` (JSON). |
| `seats` | `playtest.Seat` | Position, agent (always `jev` for new matches; `forge_ai` only on older seats), a model id (older seats only; new seats use `~typesafe/jev-latest`), the deck it plays — a user `deck` or a `metagame_deck` — `coverage` (`{"unsupported_cards": [...]}` from the bridge) and final `life`. `deck` is `SET_NULL` on delete; `deck_name` keeps the name. |
| `match_events` | `playtest.MatchEvent` | The bridge's log: `seq`, `turn`, `step` (Forge's phase), `seat_position`, `kind` (`log`/`decision`/`fallback`/`forge_ai`/`game_over`), `text`, `payload` (Forge's log category, Jev confidence and probabilities, fallback reasons), `reasoning`. |

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
