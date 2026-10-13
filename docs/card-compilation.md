# Card compilation

Before a match, every card of every seat's main deck is compiled once into a small
program of operations, then cached forever. During the game there are **zero** judge
calls. A card that cannot be compiled faithfully is *unsupported* and plays as a
blank: a refusal is a feature, an approximation is a bug.

```
raw Scryfall JSON ── characteristics() ──► CardFacts
                                            ├─ residual empty     → supported, no model call
                                            ├─ residual blocked   → unsupported, no model call
                                            └─ needs the judge
                                                 split_abilities() ─► clauses
                                                 one Jev request (op + selector per clause, ×2 orders)
                                                 parse_clause() reads the fields
                                                 validate_program() ─► CardProgram
                                                 card_rules cache (oracle_id, DSL version)
```

## 1. The deterministic pre-pass

`playtest/engine/cardrules.py` — `characteristics(card)` reads only Scryfall JSON
(`fetch_card_raw` gets it from `scryfall_cards`, so an imported deck costs no
Scryfall call):

- cost and mana value, types/subtypes/supertypes of the front face, power/toughness
  (`"1+*"` → 1, `"*"` → 0), colors;
- **mana abilities**: a `{T}: Add …` line (or the reminder text of a basic or typed
  land) yields the colors it names, always a subset of Scryfall's `produced_mana`,
  with its cost: "pay N life", "sacrifice this", a painland's damage, "activate only
  if you control an X or a Y". Mana lines with any other cost (`{1}, {T}: Add`, "any
  one color", tapping a creature) are not read;
- "enters tapped" (conservative: "unless …" counts as tapped);
- **casting options**: alternative costs ("you may pay N life and exile a blue card
  from your hand / return an Island you control rather than pay this spell's mana
  cost", optionally "if it's not your turn"; "if you control a commander, you may
  cast this spell without paying its mana cost"), "as an additional cost to cast
  this spell, sacrifice a …", "this spell can't be countered", flashback, cycling and
  basic landcycling with a mana cost. A line it cannot read whole stays in the
  residual;
- keyword lines: supported keywords (flying, trample, haste, vigilance, lifelink,
  deathtouch, first strike, double strike, menace, reach, defender, flash, hexproof,
  indestructible, ward with a mana cost) become `keywords`; prowess becomes its rules
  text ("whenever you cast a noncreature spell, this creature gets +1/+1 until end of
  turn") for the judge; any other keyword blocks the card;
- the **residual** text: what is left, one ability per line, lowercased, with the
  card's own name replaced by "this creature" / "this spell" / ….

The residual is then `empty`, `blocked` (a blocklisted mechanic: searching another
player's library, copies, "instead" — except a countered spell exiled or put on
top or into its owner's hand instead —, "for each", "as long as", protection,
"can't", exile-until, planeswalkers and sagas by type, auras, equipment, cost
modifiers, other free casting, …) or `needs_judge`. Multi-faced cards play their front face
and are `partial`.

## 2. Clauses

`playtest/engine/clauses.py` — `split_abilities(residual, facts)`, in code:

- triggers by regex: *when this enters / dies / leaves the battlefield / is put
  into a graveyard from the battlefield*, *whenever this attacks / deals combat
  damage to a player*, *enters or attacks* (two triggers), *at the beginning of
  your upkeep / end step / combat on your turn*, *whenever you / an opponent / a
  player casts a (noncreature, creature, instant or sorcery, artifact, colorless)
  spell*, *whenever another creature / a creature / a land you control enters*,
  *whenever you attack / gain life / draw a card*, *whenever an opponent draws a
  card*. Ability words ("landfall —") are dropped. Any other trigger is unreadable;
- activated abilities split on the first `:`; the cost may hold mana, `{T}`, "pay N
  life", "discard a card", "sacrifice this …" and "sacrifice a(nother) creature /
  artifact / land / …". Other mana abilities with a cost are refused;
- `Choose one —` modes on instants and sorceries become abilities sharing a
  `mode_group`;
- each effect is cut into atomic clauses at sentence ends, `then` and `and` + verb
  (a shared subject and a trailing "until end of turn" are carried over). A
  sentence, or a run of up to three, that one operation's grammar reads whole stays
  one clause ("target player reveals their hand. you choose a nonland card from it.
  that player discards that card"): the grammar states exactly what its references,
  zones and options mean;
- "you may …" marks a clause optional and "if you do, …" makes the next ones depend
  on it;
- any other clause with options, conditions, back-references, other zones,
  computed amounts or combat references is refused before any model is asked.

## 3. The DSL and the judge

`playtest/engine/dsl.py` — 29 operations: `DEAL_DAMAGE`, `GAIN_LIFE`, `LOSE_LIFE`,
`DRAW_CARDS`, `DISCARD_CARDS`, `DISCARD_CHOSEN` (Thoughtseize), `LOOK_AT_HAND`,
`DESTROY`, `EXILE`, `RETURN_TO_HAND`, `TAP`, `UNTAP`, `ADD_MANA`, `CREATE_TOKEN`,
`CREATE_ARTIFACT_TOKEN` (Treasure, Clue, Food, Blood), `MODIFY_PT`, `GRANT_KEYWORD`,
`PUT_COUNTERS`, `SACRIFICE`, `MILL`, `SEARCH_LIBRARY`, `SCRY`, `SURVEIL`, `DIG`
(look at the top N, keep K), `PUT_BACK` (Brainstorm), `ARRANGE_TOP` (Ponder),
`COUNTER_SPELL`, `COUNTER_UNLESS_PAYS`, `PAY_MANA` (only optional). Selectors are
multiplayer-aware (`ANY_TARGET`, `TARGET_CREATURE`, `EACH_OPPONENT`, `YOU`, `SELF`,
…), with restrictions (power or mana value, tapped, legendary, "an opponent
controls", a spell's mana value). Each has a one-line English description — the
option text Jev sees — and each op a small grammar.

References and computed amounts are closed too: `CONTROLLER_OF_IT` and the amounts
`POWER_OF_IT` / `TOUGHNESS_OF_IT` / `MV_OF_IT` follow (`of`) the latest targeted
object of the same ability, which the judge binds in code — a "it" with nothing
targeted before, or "it deals" after a target, is refused. `THAT_PLAYER` is the
player an event trigger was about. Ops may be `optional` ("you may") or `if_done`.

`playtest/judge.py` — `compile_card` makes **one Jev request per card**
([Agents](agents.md#jev-client)). The state is the card's name, type line and
residual text; for every clause it asks the operation and, when the clause names
one, the selector — each twice, with the options in forward and reverse order,
because Jev leans toward the first option. The card is unsupported, with a reason
naming the clause, when an answer is `NONE`, the two orders disagree, a confidence
is below `JUDGE_MIN_CONFIDENCE` (0.8), the op's grammar does not cover the clause,
the selector Jev named is not the one the text states, or `validate_program`
rejects the program. There is no repair round-trip.

`validate_program` rejects and never coerces: unknown keys, ops or selectors,
missing fields, undeclared or unused targets, unreadable costs, out-of-range
numbers, activated abilities without a cost, mana abilities doing anything else,
one-shot ops in static abilities, `X` outside spells, a restriction its selector
does not take, a reference to a target that is not a targeted object of the same
ability, "that player" outside an event about another player, "if you do" without
an optional op before it, an optional payment that is not optional, library
operations on another player's cards, and any number that does not appear in the
card's text.

## 4. The cache and coverage

`card_rules` (`decks.CardRule`) stores the validated program, status, reason and
the versioned Jev model, keyed by `oracle_id` + `DSL_VERSION`. Only Jev-answered
outcomes are cached; a card the judge could not be asked about (no key, network,
rate limit) is retried next time. Bump `DSL_VERSION` only when the vocabulary or a
program's meaning changes.

`coverage_report` weighs cards by quantity and flags an unsupported commander and
the number of dead spells (unsupported instants and sorceries). Each seat's report
is stored on `Seat.coverage`; the deck page shows it from the caches only, never
compiling during a page view.

- `manage.py card_coverage --deck <id>` or `card_coverage <decklist file>` prints a
  deck's report and every unsupported card with its reason.
- `manage.py dsl_gaps [--format F] [--top N] [decklist …]` lists **every** reason
  the metagame decks' cards (or the given lists) are not compilable, weighted by
  copies, and the cards blocked by a single reason: what extending the DSL next
  would unlock (`playtest/engine/gaps.py`).

## Extending the DSL

Adding an op or a selector means: its description and grammar in `dsl.py`, its
validation rules, its implementation in `effects.apply_op` (asking the player
through `EffectContext.ask` for any choice), a scenario test in
`tests/test_effects.py`, an entry in the anti-drift test of `tests/test_judge.py`,
the heuristic's estimate of it (`agents/heuristic._op_value`), and a `DSL_VERSION`
bump. Measure before and after with `manage.py dsl_gaps` on synced metagame decks.
