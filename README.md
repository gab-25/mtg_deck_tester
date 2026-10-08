# MTG Deck Tester

A **Django** web app where **AI agents playtest Magic: The Gathering Commander decks**.
Import your decks from a plain-text list (cards resolved through the **Scryfall** API),
seat two to four of them at a table, and let the agents play the game out — a
random baseline, a deterministic heuristic player, or a language model of your choice
through **OpenRouter**, with its reasoning written into the game log. Pages are **HTMX** over a precompiled stylesheet,
data lives in **Postgres**.

The game engine is a rules kernel — turn steps, priority and the stack, colored mana,
blocks and combat keywords, state-based actions — that doesn't play card effects yet
(see [Rules](#rules)). Its structure — engine, agents, runner — lets the rules grow
inside the engine without touching the rest.

## Features

- **Deck import**: paste a Moxfield-style list (`1 Sol Ring`, commander under a
  `Commander` header or tagged `*CMDR*`). Construction rules that plain text can
  settle are checked on the spot; the cards are then fetched from Scryfall in the
  background and checked again — commander eligibility, color identity, and the ban
  list of the deck's format. Every problem is reported at once.
- **Commander and Duel Commander**: a deck declares its format. A Commander match
  seats 2–4 players at 40 life and tracks commander damage; a Duel Commander match
  seats exactly 2 at 20 life.
- **Agents**: each seat gets its own agent — `random` (free, reproducible),
  `heuristic` (fixed rules: develop, attack when it pays, block to survive; no network,
  the same play every time) or `llm`, with an optional per-seat OpenRouter model id, so
  different models can play each other. An LLM that fails to answer, or answers with no valid choice, falls back to a
  random move and the fallback is logged: a match never stalls on a model.
- **Live game log**: a match runs in the background; its page shows the table and the
  log as they happen (HTMX polling), including the reason each LLM gave for each move.
- **Reproducible**: a match has a seed. The same seed with random or heuristic agents
  replays the same game, event for event.
- **Scryfall cache in the database**: card JSON and images are cached in Postgres
  (`scryfall_cards`, `scryfall_images`) and shared by every deck.
- **Ownership**: decks and matches belong to the user who created them; anyone
  else gets a 404.

## Rules

The engine (`playtest/engine/`) is a rules kernel. The docstring of
`playtest/engine/rules.py` is the reference — it is also the rules text the agents are
given. In short:

- **Turns** run the real steps: untap, upkeep, draw, two main phases, beginning of
  combat, declare attackers and blockers, (first-strike) combat damage, end of combat,
  end step, cleanup (discard to seven). The first player of a duel skips the first draw.
- **Priority and the stack**: the active player gets priority first, then it passes
  around the table. A spell goes on the stack and resolves only when every living
  player passes in succession. A player who can only pass passes automatically.
- **Timing**: one land per turn; lands and sorcery-speed spells only in your own main
  phase with an empty stack; instants and flash whenever you have priority.
- **Mana**: lands and rocks make the colors and amounts Scryfall says (Sol Ring makes
  two). Payment is computed exactly (max-flow) and **is not a decision**: the engine
  taps the sources least worth keeping, so a green spell never taps your only blue
  source — but no agent can choose to hold a particular land up. `{X}` spells are
  offered once per affordable X, up to 10.
- **Card effects are not simulated yet**: permanents enter the battlefield; instants
  and sorceries go to the graveyard. Keywords: flying, reach, menace, first and double
  strike, trample, deathtouch, lifelink, vigilance, haste, defender, flash,
  indestructible.
- **Combat** is declared one creature at a time, so attacks can be split across
  opponents and each defender blocks only what attacks them.
- **State-based actions** run to a fixpoint before anyone gets priority: 0 life,
  drawing from an empty library, 21 damage from one commander (Commander only), 0
  toughness, lethal damage, the legend rule. A player who loses leaves the game with
  everything they own. The commander returns to the command zone instead of staying in
  the graveyard, and costs 2 more for every previous cast.
- **Setup**: a deterministic London mulligan (keep 2–5 lands, at most two mulligans,
  the first free with three or more players) — not a decision either.
- **Limits** (`playtest/engine/limits.py`) keep every game finite: the round limit
  (then the match is a draw), decisions per turn, stack depth, state-based action
  passes, declaration steps, and 255 options per decision. Each one logs an event when
  it bites.

### Card coverage

Card effects are being compiled ahead of the game, once per card, into a small
closed effect language. A deterministic pre-pass reads mana cost, types, power and
toughness, keywords and the mana a card produces straight from Scryfall; only the
rest of the rules text goes to a judge, where code splits it into clauses and
[Jev](https://docs.typesafe.ai) (TypeSafe AI, through OpenRouter) only picks, for each
clause, which operation, which target and which amount. A card it can't read, or
can't answer confidently, is *unsupported* — never approximated. Results are cached
in the `card_rules` table. To see how much of a deck would be simulated:

```bash
uv run python manage.py card_coverage --deck <deck uuid>
uv run python manage.py card_coverage path/to/decklist.txt
```

The engine doesn't play compiled effects yet.

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
├── scryfall.py         #   Card data and image fetching
├── cache.py            #   Database and filesystem cache backends
└── importer.py         #   parse → fetch → validate, and the import job
playtest/               # App: matches between agents
├── engine/             #   The game engine (pure Python, no Django)
│   ├── cards.py        #     Card specs: the printed facts the engine plays with
│   ├── state.py        #     Steps, cards, permanents, the stack, invariants
│   ├── mana.py         #     Mana payment: max-flow feasibility, auto-tapping
│   ├── actions.py      #     The legal actions offered to an agent
│   ├── rules.py        #     The rules text; actions, combat, state-based actions
│   ├── limits.py       #     The caps that keep a game finite
│   ├── view.py         #     What a seat may see (no hidden information)
│   ├── game.py         #     Setup, steps, priority, asking agents, the result
│   ├── manacost.py     #     Mana cost parser
│   ├── cardrules.py    #     Card facts from Scryfall JSON; residual rules text
│   ├── clauses.py      #     Residual text → abilities and clauses
│   ├── dsl.py          #     The effect language and its validator
│   └── coverage.py     #     How much of a deck is simulated
├── agents/             #   Random, heuristic and LLM agents; OpenRouter and Jev clients
├── judge.py            #   Compiles cards into effect programs (Jev), cached
├── management/         #   `card_coverage` command
├── forms.py            #   The new-match form
└── runner.py           #   Stored decks → engine → stored events and result
theme/static/           # Precompiled stylesheet, vendored fonts and icons
tests/
```

## Running it

The project uses [`uv`](https://docs.astral.sh/uv/).

### Docker Compose

```bash
export OPENROUTER_API_KEY="..."       # optional: enables LLM seats and the card judge
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

## Configuration

Everything is configured through environment variables (a `.env` file is loaded in
local development; real environment variables win):

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | Postgres or SQLite URL | `postgresql://mtg:mtg@localhost:5432/mtg_tester` |
| `OPENROUTER_API_KEY` | Enables LLM seats and the card judge (Jev); without it, cards needing the judge are unsupported | — |
| `OPENROUTER_MODEL` | Default model for LLM seats | `google/gemini-2.5-flash` |
| `JEV_MODEL` | Jev model for the card judge (through OpenRouter, same key) | `~typesafe/jev-latest` |
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

The suite is hermetic: in-memory SQLite, no Scryfall and no OpenRouter calls (Jev
included). It also checks that templates only use classes the precompiled stylesheet
contains and icons the vendored font subset can render.

## License

GPL-3.0 — see [LICENSE](LICENSE).
