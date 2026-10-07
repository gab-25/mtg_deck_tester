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
- **Scryfall cache in the database**: card JSON and images are cached in Postgres
  (`scryfall_cards`, `scryfall_images`) and shared by every deck.
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
│   ├── cards.py        #     Card specs and in-game card instances
│   ├── state.py        #     Game and player state, per-format game rules
│   ├── actions.py      #     The actions offered to an agent
│   ├── rules.py        #     What the actions do; eliminations
│   ├── view.py         #     What a seat may see (no hidden information)
│   ├── game.py         #     Setup, turns, phases, asking agents, the result
│   ├── manacost.py     #     Mana cost parser
│   ├── cardrules.py    #     Card facts from Scryfall JSON; residual rules text
│   ├── clauses.py      #     Residual text → abilities and clauses
│   ├── dsl.py          #     The effect language and its validator
│   └── coverage.py     #     How much of a deck is simulated
├── agents/             #   Random agent, LLM agent, OpenRouter and Jev clients
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
