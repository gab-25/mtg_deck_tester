# Deck import

A deck is pasted as text, checked for what the text alone can settle, then its cards
are fetched from Scryfall in a background job and checked again against the real
card data. `decks/importer.py` is the one place that workflow lives.

```
create_deck (view)
  └─ decklist_errors(text)            parse + check_decklist      → 422 with every error
  └─ Deck(status=pending) + start_job(run_import, deck.id)
        run_import(deck_id)           status = processing
          └─ import_decklist(text, DbCardCache(), fmt)
               parse_decklist_text
               fetch_card_data per entry  → unresolved names? ValueError
               check_deck(cards, fmt)     → issues? ValueError
          status = ready (cards, commander, color_identity) | failed (error)
```

Every rule violation is collected and reported at once, never just the first.

## Parsing

`decks/rules/decklist.py` — `parse_decklist_text(text)` returns
`[{"quantity", "name", "is_commander"}]`.

- One card per line: `1 Sol Ring`, `1x Sol Ring`, or just `Sol Ring` (quantity 1).
- Blank lines and comments (`//…`, `#…`) are skipped.
- A bare header switches section: `Commander` / `Commanders` mark the following cards
  as commanders; `Deck`, `Main`, `Mainboard`, `Sideboard`, `Maybeboard`, `Companion`,
  `Considering` switch back to the main deck. A trailing `:` is allowed. Commander has
  no sideboard, so cards under those headers count towards the 100.
- A trailing `*CMDR*` (or `*COMMANDER*`) marks a commander inline, as Moxfield and
  Archidekt exports do.

## Legality, in two passes

`decks/rules/commander.py`, pure functions:

| Rule | `check_decklist` (names only, on submit) | `check_deck` (fetched cards, in the job) |
|---|---|---|
| Exactly 100 cards, commander included | ✓ | ✓ |
| Exactly one commander (no partner / background) | ✓ | ✓ |
| Singleton | by name: basic lands and the hand-kept `ANY_NUMBER_CARD_NAMES` are exempt | by card: basic lands and cards whose text says "a deck can have any number of cards named" are exempt; copies are counted per Scryfall id, so both spellings of a double-faced card add up |
| Commander eligibility | — | legendary creature on the front face, or "can be your commander" in its text |
| Color identity | — | every card within the commander's identity |
| Ban list of the format | — | Scryfall `legalities[<format legality_key>]` is `banned` or `not_legal` |

A missing `legalities` or `color_identity` field (cards cached by older versions)
never fails a deck: legality counts as legal, and color identity falls back to the
mana symbols on the faces.

The deck's stored `color_identity` is the union of its commanders' identities
(`deck_color_identity`).

## Fetching from Scryfall

`decks/scryfall.py` — `fetch_card_data(name, cache)`:

1. **Key**: `card_en_<slug>` of the front face (`"A // B"` → `A`), so both spellings
   of a double-faced card share one entry (`decks/text_utils.py`).
2. **Cache hit** → `process_cached_card(data)`; a cached `{"error": "not_found"}`
   returns `None`.
3. **Miss** → `GET https://api.scryfall.com/cards/named?exact=<name>` with the
   project's `User-Agent`, a 100 ms polite delay before each request, up to 5
   attempts with back-off on network errors and on `429` (honouring `Retry-After`).
   - `200` → the raw JSON is cached, then processed.
   - `404` → `{"error": "not_found"}` is cached (the card does not exist).
   - Gave up after retries or another status → `None`, **not** cached, so it is
     retried on the next import.

Any `None` makes the import fail with the list of unresolved names.

### Stored card shape

`process_cached_card` turns raw Scryfall JSON into what a `Deck` stores under
`cards[i]["data"]`:

```python
{
    "id", "name", "type_line", "cmc",
    "image_urls",      # "normal" image per printed face, on cards.scryfall.io
    "faces",           # [{"name", "mana_cost", "type_line", "rules_text"}]
    "price_eur",       # eur → eur_foil → usd×0.93 → usd_foil×0.93 → 0.0
    "power", "toughness",  # printed strings ("*", "1+*") or None, front face
    "color_identity", "produced_mana", "legalities",
}
```

The raw JSON stays in `scryfall_cards`; only this processed copy is on the deck. A
new field added here reaches newly imported decks only — existing decks need a data
migration or a safe default where the field is read.

## The card cache

`decks/cache.py` — two backends with the same `get_card(key)` / `set_card(key, data)`
interface:

- `DbCardCache` — the `scryfall_cards` table, shared by every user and deck. Writes
  are upserts, and a concurrent insert of the same key is resolved by overwriting.
  This is what the import job uses.
- `FileCardCache(dir)` — one JSON file per key under `<dir>/cards/`, for using the
  importer without a database (tests).

### Bulk loading

`manage.py load_oracle_cards` (`decks/oracle_bulk.py`) fills `scryfall_cards` from
Scryfall's Oracle Cards bulk file, so an import resolves its cards locally:

- Reads the bulk metadata, streams the gzipped JSONL to a temp file, and loads it in
  one transaction (a truncated file changes nothing), in batches of 1000 upserts.
- Uses the same `card_key` as `fetch_card_data`; skips tokens, emblems, art series
  and other layouts that never appear in a decklist, and keeps the first of several
  cards sharing a name.
- Overwrites existing rows, including cached `not_found` misses.
- Records a `BulkDataLoad` row; `--if-newer` skips the download when the stored
  `updated_at` is not older than the remote one. `--file` loads a local JSONL file
  (gzipped or not).

Usage and scheduling are in the README's *Local card database* section.

## Formats

`decks/formats.py` is the registry both apps read:

```python
FORMATS = {
    "commander": Format(label="Commander", legality_key="commander", starting_life=40,
                        min_seats=2, max_seats=4, commander_damage_limit=21),
    "duel":      Format(label="Duel Commander", legality_key="duel", starting_life=20,
                        min_seats=2, max_seats=2, commander_damage_limit=None),
}
DEFAULT_FORMAT = "commander"
```

Deck construction (100 cards, singleton, one commander) is the same for every
format; what varies is the ban list (`legality_key`) and the game (life, seats,
commander damage).

### Adding a format

1. Add an entry to `FORMATS`. `legality_key` must be a key of Scryfall's
   `legalities` object.
2. Generate migrations: `Deck.format` and `Match.format` take their `choices` from
   the registry, so both `decks` and `playtest` get one
   (`uv run python manage.py makemigrations`).
3. Nothing else is needed for a Commander-style format: the deck form, the match
   form (seat counts), the legality check and the runner (`GameRules`) all read the
   registry. The match form shows `max(max_seats)` rows.
4. A format with different **construction** rules (deck size, no commander, more
   copies) does not fit yet: `decks/rules/commander.py` and the engine's setup
   assume Commander.
5. Add tests next to the existing ones in `tests/test_commander.py` and
   `tests/test_matches_web.py`.
