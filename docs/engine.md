# Game engine

`playtest/engine/` is the rules kernel. It is pure Python — no Django, no I/O — and
every bit of randomness comes from `random.Random` instances derived from the match
seed, one per purpose (`"<seed>:shuffle"`, `"<seed>:order"`), so a game replays
event for event whenever its agents are deterministic. Nothing iterates a `set` on a
path that produces actions, log lines or payments: the log is the same across
`PYTHONHASHSEED` values.

No model is the rules engine. The kernel decides which actions are legal; an agent
only picks one of them.

## Modules

| Module | Contents |
|---|---|
| `manacost.py` | `ManaCost` (generic, pips, X) and `parse_mana_cost` / `parse_mana_symbols`. Phyrexian and `{2/W}` pips are paid with their color (never cheaper than on paper). |
| `cardrules.py` | The deterministic pre-pass: `characteristics(raw_json) -> CardFacts` (types, cost, P/T, colors, keywords, enters tapped, mana production, the *residual* rules text and its class: empty / blocked / needs the judge). See [Card compilation](card-compilation.md). |
| `clauses.py` | `split_abilities`: residual text → abilities (trigger, cost, modes) → atomic clauses with their candidate numbers and selector phrases. |
| `dsl.py` | The closed effect language: 18 ops, selectors, triggers, each with a one-line description; per-op grammars (`parse_clause`); `validate_program`; the `CardProgram` dataclasses; `describe_ability` for views and logs. |
| `coverage.py` | `coverage_report(entries)`: how much of a deck can be simulated, weighted by quantity. |
| `cards.py` | `CardSpec` (printed facts + compiled `program`, or `None` when unsupported), `card(...)` to build one by hand, `DeckSpec` (library and an optional commander). |
| `state.py` | `Step`, `GameRules`, `Card` (stable id), `Permanent` (fresh id on every zone change, CR 400.7), `StackItem`, `Player`, `GameState`, `assert_invariants`. |
| `mana.py` | Payment feasibility by exact max-flow and automatic payment (`sources`, `can_pay`, `choose_payment`, `pay`). |
| `effects.py` | Derived characteristics (static abilities), selectors and targets, the 18 operations (`apply_op`), resolution with target re-checks. |
| `zones.py` | Entering and leaving the battlefield, the command zone, a player leaving the game. |
| `actions.py` | `Action`, `ActionKind`, `legal_actions(state, seat)`. |
| `rules.py` | `apply(state, seat, action)`, stack resolution, state-based actions, combat damage, untap/cleanup, the London mulligan. **Its module docstring is the rules summary given to the LLM agent.** |
| `limits.py` | `Limits`: every cap of the kernel. |
| `events.py` | `EventKind` and `Event` (`kind`, `seat`, `text`, `payload`, `reasoning`); payloads are always JSON-safe. |
| `view.py` | `player_view(state, seat)`: the game as one seat may see it. |
| `game.py` | `Game`: setup, turns and steps, priority, triggers, asking agents, budgets, the `GameResult`. |

## A game

`Game(decks, agents, rules, *, seed, max_rounds=None, limits=None, on_event=None, clock=time.monotonic, check_invariants=False)`:

1. **Setup** — each library is shuffled; the commander, or both partners
   (Commander formats only), go to the command zone; the first player is drawn at random; every player draws a
   deterministic London mulligan hand (keep 2–5 lands, at most two mulligans, the
   first free with three or more players). No agent decides any of this.
2. **Rounds** `1..max_rounds`, one turn per living player in turn order.
3. **A turn** walks the `Step`s: untap, upkeep, draw (the first player of a
   two-player game skips it), main 1, beginning of combat, declare attackers,
   declare blockers, first-strike damage (only when a combatant has first or double
   strike), combat damage, end of combat, main 2, end step, cleanup. Combat steps
   are skipped when nobody can attack or nobody was declared.
4. The game ends when at most one player is alive (`last_standing`) or after
   `max_rounds` (`turn_limit`, a draw). A player who loses leaves the game with
   everything they own (CR 800.4a); if it was their turn, the turn ends.

### Priority

Inside each step with a priority window, the loop:

- runs state-based actions to a fixpoint at the top of every iteration (0 life,
  drew from an empty library, commander damage, toughness ≤ 0, lethal damage,
  deathtouch, the legend rule, +1/+1 and -1/-1 counters cancelling);
- puts waiting triggers on the stack in APNAP order, asking for their targets;
- asks the seat with priority — **without an agent call when `Pass` is the only
  legal action**;
- resolves the top of the stack only after every living player passed in
  succession, and is iterative, never recursive.

A player who acts keeps priority. Sorcery-speed actions need the active player's
main phase with an empty stack.

### Combat

Attackers and blockers are declared one creature at a time (`DECLARE_ATTACKER` per
creature and opponent, then `END_ATTACKERS`; each defending player in turn order
declares `DECLARE_BLOCKER` / `UNDO_BLOCKER`, then `END_BLOCKERS`). Menace withholds
`END_BLOCKERS` while an attacker has exactly one blocker. Damage is assigned in
blocker order with trample excess to the player, deathtouch makes 1 lethal,
lifelink gains its controller life, and commander combat damage is tracked per
commander card (CR 903.10a), so two partners each need 21 on their own. Each
commander also pays its own tax.

### Mana

`mana.sources` lists the untapped permanents that make mana for free (`{T}: Add`,
read by the pre-pass from Scryfall's `produced_mana`) plus floating mana.
`can_pay` is a max-flow from sources to colors to pips; `choose_payment` drops the
most valuable sources first while the rest still pays, then swaps a spent source
for an unspent one when that keeps more colors open. A source that makes more than
needed floats the rest. **Payment is automatic, never an agent decision.**

## The contract of `legal_actions`

Tested as such:

- the first action is the do-nothing one — `Pass`, `End attackers` or `End
  blockers` (menace can withhold `End blockers`);
- the order is deterministic (sorted by rank, name, ids, targets);
- every listed action is legal and applying it never raises or breaks
  `assert_invariants`;
- it is pure: it never changes the state;
- identical permanents and cards are offered once.

Spells with targets enumerate the target combinations when there are at most
`max_enumerated_targets` (8); otherwise the cast commits and `CHOOSE_TARGET`
decisions follow. `rules.apply` re-checks that an action is one of
`legal_actions` and raises `IllegalAction` otherwise: whatever an agent answers,
the kernel never applies an illegal action.

## Card effects

A card's `program` (see [Card compilation](card-compilation.md)) is a list of
abilities: spell effects, triggers (enters, dies, attacks, upkeep, end step),
activated abilities with a cost, and static abilities. Targets are chosen on cast
and checked again on resolution: a spell whose every target is gone fizzles; if only
some are, the rest still happens. Static abilities (anthems, granted keywords) are a
derived view — `effects.power`, `toughness`, `keywords` — never written onto the
permanents.

A card the compiler could not read has `program=None`: a permanent enters as a
vanilla object with its printed body and keywords; an **instant or sorcery cannot be
cast** at all, and the first time it could have been, an `unsupported_skipped`
event says so.

## Guards

`Limits` caps rounds, decisions per turn (past it everyone passes until the turn
ends, with a `decision_limit` event), stack depth, activations per permanent per
turn, state-based-action iterations, declaration steps, options per decision (255,
Jev's limit), agent calls per metered seat and the match wall clock. Every cap ends
cleanly with an event: a match runs in a daemon thread, where a hang would leave it
`running` forever.

Metered agents (`metered = True`, the Jev agent) are handed to their `fallback` past
`max_agent_calls` for that seat or `max_wall_clock_seconds` for the match, with a
`warning` event.

## What a seat can see

`player_view` returns plain data: format, round, step, active seat; the seat's own
life, available mana and colors, hand (with each card's compiled effects rendered as
short text, never the oracle text), battlefield, command zone and commander cost,
graveyard names, library count; for each living opponent their life, hand and
library counts, battlefield and commander damage; the stack; and, while a target is
being chosen, what it is for. Permanents show their current power, toughness and
keywords. Hidden information never leaves the engine.

## From stored decks to `DeckSpec`

`playtest/runner.py` compiles every seat's main deck through `playtest.judge` (or
reads a bundled benchmark), stores each seat's coverage report, and builds the
`DeckSpec`s with `CardSpec.from_facts`. `deck_spec(..., has_commander=False)` builds a
constructed deck; a Commander deck without a commander fails the match.

## Adding a rule

1. **State** — add what the rule needs to `state.py`, or a parameter to `GameRules`
   when it varies by format (then wire it in `runner.game_rules`).
2. **Actions** — offer new choices from `legal_actions` with a clear `label` and keep
   the contract above.
3. **Effects** — implement them in `rules.py` / `effects.py`, returning `Event`s.
4. **Visibility** — extend `player_view` when agents need to see it.
5. **Docs** — update the docstring of `rules.py` (it is part of the LLM's system
   prompt), the README's *Rules* section and this page.
6. **Tests** — `tests/test_engine.py` and `tests/test_effects.py` drive games with
   synthetic decks (`tests/factories.py`, `tests/decks.py`) and run with
   `check_invariants=True`.
