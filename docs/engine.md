# Forge engine

The rules are not written here. [Forge](https://github.com/Card-Forge/forge), an
open-source rules engine with nearly every card scripted, plays every game. It runs in
its own container, [`mtg_forge_engine`](https://github.com/gab-25/mtg_forge_engine)
(the *bridge*), which exposes each player decision over HTTP. This app keeps the decks,
the agents, the match page and the summary.

No model is the rules engine, and neither is this app: Forge lists the legal options
of every decision; an agent only picks one of them, or hands the decision back to
Forge's AI.

## The bridge's contract

| Method | Path | Used for |
|---|---|---|
| `GET` | `/health` | `{"status": "ready", "cards": N}`; 503 while Forge loads its card database |
| `POST` | `/games` | `{"format", "seed", "seats": [{"name", "decklist", "controller"}] × 2}` → the game, with each seat's `unsupported_cards`. Unknown cards: 422 `{"detail": {"unresolved": {"<seat>": [...]}}}` |
| `POST` | `/decks/check` | `{"format", "decklist"}` → `{"unresolved", "unsupported_cards", "missing_commander"}`; no game starts |
| `GET` | `/games/{id}` | `status` (`queued`/`running`/`finished`/`failed`/`aborted`), `turn`, `winner` (a seat index), `end_reason`, `error` |
| `GET` | `/games/{id}/events?since=N` | `{"events": [...], "next": M}` |
| `DELETE` | `/games/{id}` | abort |
| `GET` | `/decisions?wait=30&game=<id>` | the oldest pending decision of any game (or of `game`); 204 if none |
| `POST` | `/decisions/{id}` | `{"choice": i}` or `{"delegate": true}`, with an optional `meta` stored on the event; 409 when the decision is no longer pending |

A seat's `controller` is `external` (a worker answers its decisions) or `forge_ai`
(Forge's AI plays it and it never reaches a worker).

**Decisions.** A decision has a `kind` — `mulligan`, `bottom`, `priority`, `target`,
`attack`, `block`, `choose_cards`, `confirm`, `mode` — Forge's `prompt`, the seat's
filtered `view` (no hidden information) and up to 255 numbered `options`, each with a
`label` and code-computed `facts` ("deals 2 damage if unblocked", "dies if blocked by:
…"). Multi-selects (attackers, blockers, cards) come one pick at a time, option 0 being
"Done". **Everything else is always Forge's AI**: mana payment, combat damage
assignment, scry and the other choices the bridge does not expose; each is logged as a
`forge_ai` event.

**Events.** `{"seq", "turn", "phase", "seat", "kind", "text", "payload"}` with kinds:

- `log`: Forge's own game log; `payload.type` is its category (`TURN`, `PHASE`, `LIFE`,
  `STACK_ADD`, `STACK_RESOLVE`, `LAND`, `DAMAGE`, `COMBAT`, `MANA`, `ZONE_CHANGE`,
  `GAME_OUTCOME`, …);
- `decision`: a client's answer, with `kind`, `choice` and the answer's `meta`;
- `fallback`: Forge's AI decided instead; `reason` is `delegate` (the client asked,
  its `meta.fallback_kind` says why), `timeout`, `withdrawn`, `orphaned`,
  `too_many_options`, `invalid_attack`, `invalid_block` or `error`;
- `forge_ai`: a decision the bridge never exposes (`payload.method`);
- `game_over`: `winner` and `end_reason`.

**End reasons** are Forge's (`AllOpponentsLost`, `Draw`, `Conceded`, …) plus the
bridge's `turn_limit`, `aborted` and `orphaned`; `playtest.models.END_REASONS` names
them on the match page.

**Timeouts** (bridge settings, see its README): a leased decision not answered within
`DECISION_TIMEOUT` goes to Forge's AI; a game none of whose decisions is leased for
`ORPHAN_TIMEOUT` is aborted as `orphaned`; past `MAX_TURNS` the game is a draw
(`turn_limit`). A game therefore always ends, and never waits on a model.

## The client

`playtest/forge.py` is the only module that speaks HTTP to the bridge:

- `ForgeClient(url=None)` (default `FORGE_URL`, else `http://localhost:8080`) with
  `health`, `create_game`, `check_deck`, `game`, `events`, `lease`, `answer`, `abort`.
  `client()` returns one; tests monkeypatch it with a fake.
- Every failure is a `ForgeError`; unknown cards raise `ForgeDeckError`, whose
  `unresolved` maps a seat name to its cards. `answer` returns `False` on a 409.
- `decklist_text(cards, has_commander=...)` turns stored `Deck.cards` into the text
  the bridge reads (`<qty> <name>`, the commander under `Commander`, the sideboard
  under `Sideboard`).
- `bridge_format(fmt)` reads `Format.forge_format` from `decks/formats.py`
  (`duel` → `duel_commander`, constructed formats by their key). Multiplayer
  Commander has none and is refused; `check_deck` checks a Commander deck as
  `duel_commander`, since Forge knows the same cards.

## A match, end to end

1. **The view starts the game.** `create_match` stores the `Match` and its `Seat`s,
   then calls `runner.start_game`, which sends both decklists, the seed and the
   controllers, and stores the bridge's game id and each seat's coverage
   (`{"unsupported_cards": [...]}`). On `ForgeDeckError` the rows are deleted and the
   form names the cards (422); on any other `ForgeError` the same with 503. A deck
   Forge cannot play never becomes a match.
2. **The job follows it.** `runner.run_match` (a background job) marks the match
   running and copies the bridge's events into `MatchEvent` rows as they arrive,
   keeping `Match.turn` current. Transient bridge errors are retried (5 in a row);
   a deleted match or a game past `MATCH_TIMEOUT` (3 hours) is aborted.
3. **When the game ends** it stores the winner, Forge's end reason, each seat's final
   life and the summary. Status: `finished`; `stopped` when aborted by the user;
   `failed` when Forge failed or the game was `orphaned`.

A running match can be stopped from its page (`matches/<id>/stop`) and deleting one
aborts its game first.

## The worker pool

`playtest/workers.py`: `FORGE_WORKERS` threads (default 4) start once per process,
with the first match. Each long-polls `lease(30)`, finds the `Match` by its
`forge_game_id` and the `Seat` by position, asks the seat's agent and answers. The
pool size bounds the model calls in flight across every running match.

- Agents live in an `AgentCache`, one per (match, seat), for the whole game: a game's
  decisions come one at a time, so a seat's seeded RNG is consumed in order and a
  random seat replays from its seed.
- A decision for a game not yet linked to a match waits up to 5 seconds for the link
  (the bridge starts a game before the view stores its id), then is delegated.
- Model-backed agents are metered: past `MAX_AGENT_CALLS` (400) for the seat or
  `MAX_MATCH_SECONDS` (1800) since the match started, the seat delegates every
  remaining decision (`fallback_kind = "budget"`).
- An agent's exception, a seat with no agent or an option out of range is delegated
  (`fallback_kind = "error"`); the worker keeps running. Bridge errors are logged and
  retried after a pause. If a worker dies anyway, `DECISION_TIMEOUT` hands its
  decision to Forge's AI: no match stalls.

With `FORGE_WORKERS=0` (and in tests, where jobs run inline) no pool runs: each
match's runner answers its own decisions between reads of the log.

## Reproducibility

The bridge replays a game from its seed when the answers are the same, so random and
Forge-AI seats replay exactly — every event but Forge's wall-clock line
("Match Duration …"). Jev seats are as reproducible as Jev.

## The summary

`playtest/summary.py::summarize_match(events, seats, fmt, game)` is pure and
deterministic. It reads Forge's log by the names the seats played under
(`table_names`: the deck's name, plus the seat when a deck sits twice):

| Log line | Category | Counts |
|---|---|---|
| `Turn 3 (Red)` | `TURN` | the turns each seat played |
| `Life: Green 20 > 17` | `LIFE` | life over time and final life |
| `Red cast Lightning Bolt targeting [Green]` | `STACK_ADD` | spells cast (`triggered` / `activated` lines are not spells) |
| `Red played Mountain (31)` | `LAND` | lands played |

From the bridge's own events it counts, per seat, the agent's decisions against
Forge AI's (fallbacks and `forge_ai` events), the median confidence, low-confidence
fallbacks and fallbacks by reason (`fallback_reason`: the agent's `fallback_kind`
when it delegated, else the bridge's reason). A fallback is followed by a `forge_ai`
event for the very decision Forge's AI then takes; `fallback_executions` pairs the
two so the decision counts (and shows) once. `automatic` counts the non-exposed
decisions by Forge method. The match page hides `PHASE` and `MANA` log lines and
folds runs of `forge_ai` events into one line.

## Limitations

- Two seats per game: multiplayer Commander waits for a bridge extension.
- The bridge keeps no state across restarts: running matches then fail.
- With several web processes (gunicorn workers), each runs its own pool, so one
  game's decisions may be answered by agents in different processes and a random
  seat no longer replays exactly.
