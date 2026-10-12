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
- **mana production**: a free `{T}: Add …` line (or the reminder text of a basic or
  typed land) yields the colors it names, always a subset of Scryfall's
  `produced_mana`. A line with a cost (`{1}, {T}: Add`), a condition (`for each`) or
  a side effect (a painland's damage) is not a free source;
- "enters tapped" (conservative: "unless …" counts as tapped);
- keyword lines: supported keywords (flying, trample, haste, vigilance, lifelink,
  deathtouch, first strike, double strike, menace, reach, defender, flash, hexproof,
  indestructible, ward with a mana cost) become `keywords`; any other keyword blocks
  the card;
- the **residual** text: what is left, one ability per line, lowercased, with the
  card's own name replaced by "this creature" / "this spell" / ….

The residual is then `empty`, `blocked` (a blocklisted mechanic: library search,
copies, "instead", "for each", "as long as", protection, "can't", exile-until,
planeswalkers and sagas by type, auras, equipment, cost modifiers, free or
alternative casting, …) or `needs_judge`. Multi-faced cards play their front face
and are `partial`.

## 2. Clauses

`playtest/engine/clauses.py` — `split_abilities(residual, facts)`, in code:

- triggers by regex: *when this enters*, *when this dies*, *whenever this attacks*,
  *at the beginning of your upkeep / end step*; any other trigger is unreadable;
- activated abilities split on the first `:`; the cost may hold mana, `{T}` and
  "sacrifice this …". Mana abilities with a cost are refused (payment is automatic
  and only uses free sources);
- `Choose one —` modes on instants and sorceries become abilities sharing a
  `mode_group`;
- each effect is cut into atomic clauses at sentence ends, `then` and `and` + verb
  (a shared subject and a trailing "until end of turn" are carried over);
- clauses with options, conditions, back-references, other zones, computed amounts
  or combat references are refused before any model is asked.

## 3. The DSL and the judge

`playtest/engine/dsl.py` — 18 operations (`DEAL_DAMAGE`, `GAIN_LIFE`, `LOSE_LIFE`,
`DRAW_CARDS`, `DISCARD_CARDS`, `DESTROY`, `EXILE`, `RETURN_TO_HAND`, `TAP`, `UNTAP`,
`ADD_MANA`, `CREATE_TOKEN`, `MODIFY_PT`, `GRANT_KEYWORD`, `PUT_COUNTERS`, `SACRIFICE`,
`MILL`, `COUNTER_SPELL`) and multiplayer-aware selectors (`ANY_TARGET`,
`TARGET_CREATURE`, `EACH_OPPONENT`, `YOU`, `SELF`, …). Each has a one-line English
description — the option text Jev sees — and each op a small grammar.

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
one-shot ops in static abilities, `X` outside spells, and any number that does not
appear in the card's text.

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

## Extending the DSL

Adding an op or a selector means: its description and grammar in `dsl.py`, its
validation rules, its implementation in `effects.apply_op`, a scenario test in
`tests/test_effects.py`, an entry in the anti-drift test of `tests/test_judge.py`,
and a `DSL_VERSION` bump.
