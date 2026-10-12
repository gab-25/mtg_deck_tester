# Game engine

`playtest/engine/` plays a deliberately simplified game of Commander. It is pure
Python — no Django, no I/O — and its only randomness is the seeded
`random.Random` owned by the `Game`, so a game is reproducible whenever its agents
are.

## Modules

| Module | Contents |
|---|---|
| `cards.py` | `Kind` (`land`, `creature`, `permanent`, `spell`), `CardSpec` (the printed facts: name, kind, mana value, power, toughness), `DeckSpec` (commander + library), `CardInstance` (one physical card with `uid`, `tapped`, `summoning_sick`, `is_commander`), `Uids`. |
| `state.py` | `GameRules` (per-format parameters: starting life, commander-damage limit, opening hand, lands per turn, commander tax), `PlayerState` (zones, life, commander casts, land drops, commander damage taken, elimination round), `GameState` (players, round, turn, active seat, phase). |
| `actions.py` | `Phase` (`main`, `combat`), `ActionKind`, the frozen `Action` dataclass, `legal_actions(state, seat, phase)` and `commander_cost`. |
| `rules.py` | What actions do: `apply`, `begin_turn` (untap), `draw`, `eliminate`, combat. Raises `IllegalAction`. **Its module docstring is the rules summary given to the LLM agent** (see below). |
| `events.py` | `EventKind` and the `Event` dataclass (`kind`, `seat`, `text`, `payload`, `reasoning`). |
| `view.py` | `player_view(state, seat)`: the JSON-ready game as one seat may see it. |
| `game.py` | `Game`: setup, the turn loop, asking agents, recording events, the `GameResult`. |

## The turn loop

`Game(decks, agents, rules, seed=..., max_rounds=20, max_decisions_per_turn=30, on_event=None)`:

1. **Setup** — each library is shuffled with the game's RNG, the commander goes to
   the command zone, and the opening hand (7) is drawn. No mulligans.
2. **`run()`** picks the first player at random, then plays rounds `1..max_rounds`.
   A round is one turn for every player still alive, in seat order starting from the
   first player.
3. **A turn** (`_take_turn`):
   - `begin_turn`: untap, summoning sickness ends, land drops reset.
   - Draw — except the very first turn of a two-player game (CR 103.8a). Drawing
     from an empty library eliminates the player.
   - **Main phase**, then **combat phase**. In each, the engine computes
     `legal_actions`; if the only option is `pass` it moves on without calling the
     agent. Otherwise it asks the agent, applies the action, and asks again, until
     the agent passes (main) or has attacked once (combat).
   - After `max_decisions_per_turn` decisions in one turn the engine logs
     `decision_limit` and ends the turn, so a looping agent cannot stall a game.
4. The game ends when at most one player is alive (`last_standing`) or after
   `max_rounds` (`turn_limit`, a draw).

Every event goes through `_record`, which stamps it with `seq`, `round` and `turn`
(a `RecordedEvent`) and calls `on_event`. The runner uses that callback to write
`MatchEvent` rows while the game runs.

### Asking an agent

`_decide` calls `agent.choose(player_view(state, seat), options)` and gets back a
`Decision`. Then:

- if `decision.fallback_reason` is set, an `agent_fallback` event is logged;
- an action that is not one of `options` raises `IllegalAction` (the match fails);
- a pass, or any decision with `reasoning`, is logged as `pass` / `decision` with
  the reasoning attached — that is how an LLM's explanation reaches the match log.

## What a seat can see

`player_view` returns plain data: the seat's own hand, battlefield, command zone,
current commander cost, graveyard names, library count, land drops and untapped
lands; and for each living opponent their life, hand and library **counts**,
battlefield, command zone, and the commander damage this seat has dealt them.
Hidden information (opponents' hands, every library's order) never leaves the
engine.

## Rules supported

Kept in sync with the README's *Rules* section and with the docstring of
`playtest/engine/rules.py`:

- Opening hand of seven, no mulligans; the commander starts in the command zone.
- Untap, draw (first player of a two-player game skips the first draw), one land per
  turn, then one attack.
- Every land taps for one generic mana — colors are ignored.
- A spell can be cast when untapped lands cover its mana value. Creatures and other
  permanents enter the battlefield (creatures with summoning sickness); instants and
  sorceries go to the graveyard with no effect.
- The commander is cast from the command zone for its mana value plus the commander
  tax (2 per previous cast). Once cast it stays on the battlefield: nothing returns
  it to the command zone.
- An attack sends every untapped, non-sick creature at one opponent. No blockers:
  each attacker deals damage equal to its power. Commander combat damage is tracked
  per attacking seat.
- A player is eliminated at 0 life, at the format's commander-damage limit (21 in
  Commander, untracked in Duel Commander), or when drawing from an empty library.

**Not supported:** the stack and priority, instant-speed play, blocking, activated
and triggered abilities, any card effect, colored mana, mulligans, toughness /
creature death, partner or background commanders.

### From stored cards to `CardSpec`

`playtest/runner.py` builds the engine's cards from a deck's stored Scryfall data:

- `Kind` from `decks.rules.cards.classify_card` (front face): Land → `land`,
  Creature → `creature`, Artifact / Enchantment / Planeswalker / Battle →
  `permanent`, everything else → `spell`.
- `mana_value` is Scryfall's `cmc` truncated to an int.
- `power` / `toughness` take the leading integer of the printed value
  (`"3"` → 3, `"1+*"` → 1, `"*"` → 0).
- The first card flagged `is_commander` becomes the commander; a deck without one
  fails the match.

## Adding a rule

The engine is built so a rule can be added without touching agents, the runner or
the UI. The usual path:

1. **State** — add whatever the rule needs to `PlayerState` / `GameState` /
   `CardInstance`, or a parameter to `GameRules` if it varies by format.
2. **Actions** — if players get a new choice, add an `ActionKind` and offer it from
   `legal_actions` with a clear `label` (agents read labels; the LLM sees them as its
   numbered options). Keep `pass` last.
3. **Effects** — implement it in `rules.py`: extend `apply`, raise `IllegalAction`
   for anything an agent should not have been offered, and return `Event`s for what
   happened (add an `EventKind` if it is a new kind of thing).
4. **Turn structure** — only if the rule adds a step or phase, change
   `Game._take_turn`.
5. **Visibility** — if agents need to see the new state, add it to `player_view`,
   keeping hidden information hidden.
6. **Docs** — update the docstring of `rules.py` (it is part of the LLM's system
   prompt), the README's *Rules* section and the list above.
7. **Tests** — `tests/test_engine.py` drives games with the synthetic decks in
   `tests/factories.py` (`engine_deck`, `bear`, `FOREST`) and scripted agents.

The random agent needs no change: it picks among whatever `legal_actions` returns.
The runner needs a change only when the rule needs a card fact `CardSpec` does not
carry yet — then extend `CardSpec`, fill it in `runner.card_spec`, and make sure the
fact is in the stored card data (`decks.scryfall.process_cached_card`). Decks
imported before that change lack the new field: read it with a safe default, or
backfill it with a data migration as `decks/migrations/0002_card_image_urls.py`
does.

A new format parameter goes in `decks.formats.Format`, then in `GameRules`, and is
wired through `runner.game_rules`.
