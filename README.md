# MTG Deck Tester

A **Django** web app where **AI agents playtest competitive Magic: The Gathering decks**:
Standard, Pioneer, Modern, Legacy, Vintage, Pauper, Commander (cEDH included) and Duel
Commander. Import your decks from a plain-text list (cards resolved through the
**Scryfall** API), seat them at a table — or pit them against bundled benchmark decks — and
let the agents play the game out under real rules: the stack and priority, colored mana,
blockers, and each card's own effects. Pages are **HTMX** over a precompiled stylesheet,
data lives in **Postgres**.

Card effects are **compiled once** from oracle text into a small, closed effect language,
and the seats are played by a deterministic heuristic agent or by
[Jev](https://docs.typesafe.ai/introduction), TypeSafe AI's System One model, which picks
among the legal actions and says how sure it is. No model is ever the rules engine: the
rules live in deterministic Python that decides which actions are legal; a model only
picks one of them.

## Features

- **Deck import**: paste a Moxfield-style list (`4 Lightning Bolt`; a commander under a
  `Commander` header or tagged `*CMDR*`; a sideboard under a `Sideboard` header).
  Construction rules that plain text can settle are checked on the spot; the cards are
  then fetched from Scryfall in the background and checked again — copy limits,
  commander eligibility and color identity, and the ban list of the deck's format.
  Every problem is reported at once.
- **Formats**: a deck declares its format, and is checked against that format's
  construction rules and ban list:

  | Format | Players | Life | Deck | Copies | Commander |
  |---|---|---|---|---|---|
  | Standard, Pioneer, Modern, Legacy, Pauper | 2 | 20 | 60+ main, ≤15 sideboard | 4 | — |
  | Vintage | 2 | 20 | 60+ main, ≤15 sideboard | 4 (restricted: 1) | — |
  | Commander (cEDH included) | 2–4 | 40 | exactly 100 | 1 | yes, 21 damage |
  | Duel Commander | 2 | 20 | exactly 100 | 1 | yes |

  A constructed deck lists its sideboard under a `Sideboard` header; the sideboard is
  imported and checked, but a match is a single game played with the main deck.
- **Agents**: each seat gets its own agent — `heuristic` (fixed rules, no network),
  `jev` (Jev picks each action through OpenRouter; its confidence and runner-up actions
  are logged), `llm` (a language model of your choice through OpenRouter, with its
  written reason), or `random` (a free baseline). A Jev seat that is unsure (below
  `JEV_MIN_CONFIDENCE`) or unreachable hands the decision to the heuristic agent, and a
  match never stalls on a model.
- **Compiled card effects**: before a match, every card is compiled into a program of
  operations (damage, life, cards, removal, counterspells, tokens, pump, counters,
  mana…) and cached forever in `card_rules`. A card the compiler cannot express is
  *unsupported* and plays as a blank — see [How cards are compiled](#how-cards-are-compiled).
- **Coverage**: every seat shows how much of its deck the simulation could play, with
  warnings for an unsupported commander and for dead spells, and the list of
  unsupported cards with their reasons. The deck page shows it too once every card is
  in the cache.
- **Benchmark decks**: bundled opponents (aggro, midrange, control) built to compile
  at 100% coverage: three 100-card decks for the Commander formats and three 60-card
  decks for the constructed ones.
- **Live game log and summary**: a match runs in the background; its page shows the
  table and the log as they happen (HTMX polling), each Jev decision with its
  confidence, and at the end a deterministic summary per seat (damage by source,
  spells cast, commander tax, lands, dead spells, decision confidence).
- **Reproducible**: a match has a seed. The same seed with heuristic or random agents
  replays the same game, event for event: there are no model calls during a game
  except the seats' own decisions.
- **Scryfall cache in the database**: card JSON is cached in Postgres
  (`scryfall_cards`) and shared by every deck. Card images are not stored: the
  browser loads them straight from Scryfall's CDN (`cards.scryfall.io`). The card table can be
  filled in one go from Scryfall's bulk data (see [Local card database](#local-card-database)).
- **Ownership**: decks and matches belong to the user who created them; anyone
  else gets a 404.

## Rules

The kernel (`playtest/engine/`) plays 2-player constructed games, 2–4 player Commander
and 2-player Duel Commander. Everything below applies to every format; the commander
rules apply to the Commander formats only.

- **Turns and priority**: untap, upkeep, draw, main, combat (beginning, attackers,
  blockers, first-strike damage, damage, end), second main, end step, cleanup. The
  first player of a two-player game skips their first draw. Priority passes around
  the table in turn order; the top of the stack resolves only when every player still
  in the game passes in succession.
- **Mana**: lands and rocks make the colors Scryfall lists for them (Sol Ring makes
  two). Costs are checked with an exact max-flow, and paid **automatically**, keeping
  the most colors open — no agent decides how to pay, so "holding up an Island" is not
  a decision anyone makes.
- **Spells and abilities**: one land per turn; sorcery-speed cards in your main phase
  with an empty stack; instants and flash any time you have priority. Targets are
  chosen on cast and checked again on resolution. Triggered abilities (enters, dies,
  attacks, upkeep, end step) go on the stack in APNAP order. Static abilities
  (anthems, granted keywords) are applied as a derived view.
- **Combat**: attackers and blockers are declared one at a time, each attacker at one
  opponent, each defender blocking what attacks them. Flying, reach, menace, first
  and double strike, trample, deathtouch, lifelink, vigilance, haste, defender,
  hexproof, indestructible and ward (with a mana cost) are supported.
- **Commander**: the commander starts in the command zone, costs 2 more per previous
  cast, returns to the command zone instead of dying or being exiled, and deals
  commander damage (21 eliminates, Commander only).
- **Losing**: 0 life, drawing from an empty library, or the commander-damage limit. A
  player who loses leaves with everything they own. The last player standing wins; a
  match that reaches its round limit is a draw.
- **Opening hands**: a deterministic London mulligan for everyone (keep 2–5 lands, at
  most two mulligans, the first free with three or more players).
- **Guards**: caps on rounds, decisions per turn, stack depth, activations,
  state-based-action iterations, declaration steps, options per decision (255),
  model calls per seat and match wall clock. Each one ends cleanly with an event.

Not supported: layers, replacement and copy effects, planeswalkers, auras and
equipment, alternative costs, and choosing how to pay mana.

## How cards are compiled

1. **Deterministic pre-pass** (`engine/cardrules.py`): cost, types, power/toughness,
   colors, keywords, "enters tapped" and the mana a card makes come straight from the
   cached Scryfall JSON. What is left of the rules text is the *residual*: empty means
   supported with no model call; a match on a blocklist (library search, copies,
   "instead", "for each"…) means unsupported with no model call.
2. **Clause splitter** (`engine/clauses.py`): code reads the trigger, the cost, the
   modes and the atomic clauses, and refuses what it cannot read (options, conditions,
   back-references, other zones).
3. **Judge** (`judge.py`): one Jev request per card. For each clause Jev picks the
   operation and the selector, each asked twice with the options in opposite orders.
   The op's fields are then read off the clause by its grammar, and the program goes
   through the validator (`engine/dsl.py`), which rejects and never coerces. A `NONE`
   answer, a disagreement, low confidence or a rejected program make the card
   unsupported, with a reason naming the clause.
4. **Cache**: results are stored in `card_rules` by oracle id and DSL version, so a card
   is compiled once; a card the judge could not be asked about is retried next time.

`manage.py card_coverage --deck <id>` (or `card_coverage <decklist file>`) prints a
deck's coverage report, and `manage.py build_benchmarks` regenerates the benchmark
decks' `.rules.json` from their decklists in `playtest/benchmarks/`.

## Documentation

- [Architecture](docs/architecture.md) — how the apps fit together, the request →
  background job → stored result flow, the data model and the boundaries.
- [Game engine](docs/engine.md) — state, actions, rules, per-seat views, the turn
  loop, and how to add a rule.
- [Agents](docs/agents.md) — the agent interface, the random and LLM agents, the
  OpenRouter client and the fallback.
- [Deck import](docs/deck-import.md) — parsing, Scryfall fetching, the card cache,
  legality checks and the format registry.
- [Development](docs/development.md) — running locally and with Docker, tests,
  template/asset constraints, migrations.

## Project structure

```
manage.py
mtg_deck_tester/        # Django project: settings, URLs, layout templates, home page
├── jobs.py             #   Background jobs (thread per job; inline in tests)
├── access.py           #   Ownership check shared by every view
└── logging_context.py  #   Stamps "[match <id>]" / "[deck <id>]" on job logs
decks/                  # App: decks and the Scryfall cache
├── rules/              #   Decklist parsing, card types, Commander and constructed legality (pure)
├── formats.py          #   Every format's parameters and family
├── scryfall.py         #   Card data fetching
├── cache.py            #   Database and filesystem cache backends
└── importer.py         #   parse → fetch → validate, and the import job
playtest/               # App: matches between agents
├── engine/             #   The rules kernel (pure Python, no Django)
│   ├── cardrules.py    #     Deterministic pre-pass over Scryfall JSON
│   ├── clauses.py      #     Rules text -> abilities and atomic clauses
│   ├── dsl.py          #     The closed effect language and its validator
│   ├── coverage.py     #     Per-deck coverage report
│   ├── manacost.py     #     Mana cost parsing
│   ├── mana.py         #     Max-flow payment feasibility and auto-tapping
│   ├── state.py        #     Steps, cards, permanents, stack, players
│   ├── actions.py      #     The legal actions offered to an agent
│   ├── effects.py      #     DSL operations, targets, static abilities
│   ├── zones.py        #     Zone changes, the command zone, leaving the game
│   ├── rules.py        #     Applying actions, the stack, state-based actions, combat
│   ├── limits.py       #     The kernel's guards
│   ├── events.py       #     What the engine reports (the match log)
│   ├── view.py         #     What a seat may see (no hidden information)
│   └── game.py         #     Setup, turns, priority, triggers, asking agents
├── agents/             #   Heuristic, Jev, LLM and random agents; Jev and OpenRouter clients
├── benchmarks/         #   Bundled benchmark decks and their compiled rules
├── judge.py            #   Compiles cards with Jev, through the card_rules cache
├── summary.py          #   The deterministic match summary
├── forms.py            #   The new-match form
└── runner.py           #   Stored decks -> compile -> engine -> stored events and result
theme/static/           # Precompiled stylesheet, vendored fonts and icons
tests/
docs/                   # Project documentation (see Documentation above)
CLAUDE.md               # Notes for Claude Code
```

## Running it

The project uses [`uv`](https://docs.astral.sh/uv/).

### Docker Compose

```bash
export OPENROUTER_API_KEY="..."       # optional: enables the card judge, Jev and LLM seats
export OPENROUTER_MODEL="..."         # optional: default LLM (google/gemini-2.5-flash)
export JEV_MODEL="..."                # optional: Jev model (~typesafe/jev-latest)
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
| `OPENROUTER_API_KEY` | Enables Jev (the card judge and Jev seats) and LLM seats; without it, cards that need the judge are unsupported | — |
| `OPENROUTER_MODEL` | Default model for LLM seats | `google/gemini-2.5-flash` |
| `JEV_MODEL` | Jev model, for the judge and Jev seats | `~typesafe/jev-latest` |
| `JEV_MIN_CONFIDENCE` | Below this confidence a Jev seat hands the decision to the heuristic agent | `0.2` |
| `SECRET_KEY` / `DEBUG` | Django basics | dev key / on |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins behind a proxy | — |
| `RUN_JOBS_IN_BACKGROUND` | Run imports and matches off the request thread | `1` |
| `HOST` / `PORT` / `RELOAD` | Local server (`python -m mtg_deck_tester`) | `0.0.0.0` / `8000` / off |
| `LOG_LEVEL` | Console log level | `INFO` |

Jev is reached through OpenRouter (`/api/v1/systemone`), so one key covers everything;
no TypeSafe account is needed. A Jev or LLM seat asks its model once per real decision
(the engine passes on its own when passing is the only option), and a match can take a
few hundred decisions: a seat is handed to the heuristic agent past 400 calls or five
minutes per match. Keep an eye on the round limit and on your OpenRouter spend.

## Tests

```bash
uv run pytest
```

The suite is hermetic: in-memory SQLite, no Scryfall and no OpenRouter calls (a guard
fails any test that reaches the network), and no API key. It also
checks that templates only use classes the precompiled stylesheet contains and icons
the vendored font subset can render.

## License

GPL-3.0 — see [LICENSE](LICENSE).
