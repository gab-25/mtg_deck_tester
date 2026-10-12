# MTG Deck Tester

A **Django** web app where **AI agents playtest Magic: The Gathering Commander decks**.
Import your decks from a plain-text list (cards resolved through the **Scryfall** API),
seat two to four of them at a table, and let the agents play the game out — a
random baseline, or a language model of your choice through **OpenRouter**, with its
reasoning written into the game log. Pages are **HTMX** over a precompiled stylesheet,
data lives in **Postgres**.

This is the project's skeleton: the game engine plays a deliberately **simplified**
version of Magic (see [Rules](#rules)). Its structure — engine, agents, runner — is
built so real rules can be added inside the engine without touching the rest.

## Features

- **Deck import**: paste a Moxfield-style list (`1 Sol Ring`, commander under a
  `Commander` header or tagged `*CMDR*`). Construction rules that plain text can
  settle are checked on the spot; the cards are then fetched from Scryfall in the
  background and checked again — commander eligibility, color identity, and the ban
  list of the deck's format. Every problem is reported at once.
- **Commander and Duel Commander**: a deck declares its format. A Commander match
  seats 2–4 players at 40 life and tracks commander damage; a Duel Commander match
  seats exactly 2 at 20 life.
- **Agents**: each seat gets its own agent — `random` (free, reproducible) or `llm`,
  with an optional per-seat OpenRouter model id, so different models can play each
  other. An LLM that fails to answer, or answers with no valid choice, falls back to a
  random move and the fallback is logged: a match never stalls on a model.
- **Live game log**: a match runs in the background; its page shows the table and the
  log as they happen (HTMX polling), including the reason each LLM gave for each move.
- **Reproducible**: a match has a seed. The same seed with random agents replays the
  same game, event for event.
- **Scryfall cache in the database**: card JSON is cached in Postgres
  (`scryfall_cards`) and shared by every deck. Card images are not stored: the
  browser loads them straight from Scryfall's CDN (`cards.scryfall.io`). The card table can be
  filled in one go from Scryfall's bulk data (see [Local card database](#local-card-database)).
- **Ownership**: decks and matches belong to the user who created them; anyone
  else gets a 404.

## Rules

The engine (`playtest/engine/`) supports:

- An opening hand of seven, no mulligans; the commander starts in the command zone.
- Untap, draw (the first player of a two-player game skips the first draw), one land
  per turn, then one attack.
- Every land taps for one generic mana — colors are ignored.
- A spell can be cast when untapped lands cover its mana value. Creatures and other
  permanents enter the battlefield; instants and sorceries go to the graveyard with
  no effect.
- The commander is cast from the command zone for its mana value plus the commander
  tax (2 per previous cast).
- An attack sends every creature that can attack at one opponent. There are no
  blockers: each attacker deals damage equal to its power.
- A player is eliminated at 0 life, at 21 damage from a single commander (Commander
  only), or when drawing from an empty library. The last player standing wins; a
  match that reaches its round limit is a draw.

Not supported yet: the stack and priority, instant-speed play, blocking, abilities
and card effects, colored mana, mulligans.

## Project structure

```
manage.py
mtg_deck_tester/        # Django project: settings, URLs, layout templates, home page
├── jobs.py             #   Background jobs (thread per job; inline in tests)
├── access.py           #   Ownership check shared by every view
└── logging_context.py  #   Stamps "[match <id>]" / "[deck <id>]" on job logs
decks/                  # App: decks and the Scryfall cache
├── rules/              #   Decklist parsing, card types, Commander legality (pure)
├── formats.py          #   Commander / Duel Commander parameters
├── scryfall.py         #   Card data fetching
├── cache.py            #   Database and filesystem cache backends
└── importer.py         #   parse → fetch → validate, and the import job
playtest/               # App: matches between agents
├── engine/             #   The game engine (pure Python, no Django)
│   ├── cards.py        #     Card specs and in-game card instances
│   ├── state.py        #     Game and player state, per-format game rules
│   ├── actions.py      #     The actions offered to an agent
│   ├── rules.py        #     What the actions do; eliminations
│   ├── view.py         #     What a seat may see (no hidden information)
│   └── game.py         #     Setup, turns, phases, asking agents, the result
├── agents/             #   Random agent, LLM agent, OpenRouter client
├── forms.py            #   The new-match form
└── runner.py           #   Stored decks → engine → stored events and result
theme/static/           # Precompiled stylesheet, vendored fonts and icons
tests/
```

## Running it

The project uses [`uv`](https://docs.astral.sh/uv/).

### Docker Compose

```bash
export OPENROUTER_API_KEY="..."       # optional: enables LLM seats
export OPENROUTER_MODEL="..."         # optional: default model (google/gemini-2.5-flash)
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

Then open <http://localhost:8000>.

### Locally

```bash
uv sync
cp .env.example .env                  # then edit it
uv run python manage.py createsuperuser
uv run python -m mtg_deck_tester      # migrate + runserver
```

## Local card database

Load every card from Scryfall's [Oracle Cards](https://scryfall.com/docs/api/bulk-data)
bulk file into `scryfall_cards`, so importing a deck resolves its cards locally instead
of calling the Scryfall API once per card:

```bash
uv run python manage.py load_oracle_cards                  # download (~25 MB gzipped) and load
uv run python manage.py load_oracle_cards --if-newer       # skip if already up to date
uv run python manage.py load_oracle_cards --file oracle-cards.jsonl.gz   # offline, local file (JSONL, gzipped or not)
```

With Docker: `docker compose exec web python manage.py load_oracle_cards`.

A second run updates the cards it already has. Cards missing from the table (spoiled
after the last load, a typo) still go through the Scryfall API.

Scryfall regenerates the file daily, and the prices it carries are those of the download
day. Run the command **daily or weekly** with `--if-newer` to keep cards and prices
current, e.g. from cron:

```cron
30 4 * * * cd /path/to/mtg_deck_tester && uv run python manage.py load_oracle_cards --if-newer
```

## Configuration

Everything is configured through environment variables (a `.env` file is loaded in
local development; real environment variables win):

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | Postgres or SQLite URL | `postgresql://mtg:mtg@localhost:5432/mtg_tester` |
| `OPENROUTER_API_KEY` | Enables LLM seats | — |
| `OPENROUTER_MODEL` | Default model for LLM seats | `google/gemini-2.5-flash` |
| `SECRET_KEY` / `DEBUG` | Django basics | dev key / on |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins behind a proxy | — |
| `RUN_JOBS_IN_BACKGROUND` | Run imports and matches off the request thread | `1` |
| `HOST` / `PORT` / `RELOAD` | Local server (`python -m mtg_deck_tester`) | `0.0.0.0` / `8000` / off |
| `LOG_LEVEL` | Console log level | `INFO` |

An LLM seat asks the model once per decision, and a match can take a few hundred
decisions: keep an eye on the round limit and on your OpenRouter spend.

## Tests

```bash
uv run pytest
```

The suite is hermetic: in-memory SQLite, no Scryfall and no OpenRouter calls. It also
checks that templates only use classes the precompiled stylesheet contains and icons
the vendored font subset can render.

## License

GPL-3.0 — see [LICENSE](LICENSE).
