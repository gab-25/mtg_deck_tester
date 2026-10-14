# Deck import

A deck is pasted as text, checked for what the text alone can settle, then its cards
are fetched from Scryfall in a background job and checked again against the real
card data. `decks/importer.py` is the one place that workflow lives.

```
create_deck (view)
  └─ decklist_errors(text, fmt)       parse + check_decklist      → 422 with every error
  └─ Deck(status=pending) + start_job(run_import, deck.id)
        run_import(deck_id)           status = processing
          └─ import_decklist(text, DbCardCache(), fmt)
               parse_decklist_text(text, fmt)
               fetch_card_data per entry  → unresolved names? ValueError
               check_deck(cards, fmt)     → issues? ValueError
          status = ready (cards, commander, color_identity) | failed (error)
```

Every rule violation is collected and reported at once, never just the first.

## Parsing

`decks/rules/decklist.py` — `parse_decklist_text(text, fmt)` returns
`[{"quantity", "name", "is_commander"}]`, plus `"board"` (`"main"` / `"side"`) in
constructed formats.

- One card per line: `1 Sol Ring`, `1x Sol Ring`, or just `Sol Ring` (quantity 1).
- Blank lines and comments (`//…`, `#…`) are skipped.
- A bare header switches section (a trailing `:` is allowed):
  - `Commander` / `Commanders` mark the following cards as commanders; a trailing
    `*CMDR*` (or `*COMMANDER*`) marks one inline, as Moxfield and Archidekt exports do.
  - **Commander formats**: `Deck`, `Main`, `Mainboard`, `Sideboard`, `Maybeboard`,
    `Companion`, `Considering` switch back to the deck. Commander has no sideboard,
    so cards under those headers count towards the 100.
  - **Constructed formats**: `Sideboard` cards get `board="side"`; `Deck` / `Main`
    switch back; `Maybeboard`, `Considering` and `Companion` are skipped (Arena lists
    the companion again in its sideboard). A list with **no headers at all** (MTGO,
    MTGGoldfish) puts the sideboard after the first blank line.

## Legality, in two passes

`decks/rules/legality.py` is the single entry point — `check_decklist(entries, fmt)`
on submit (names only) and `check_deck(cards, fmt)` in the job — and dispatches on
the format's family to `rules/commander.py` or `rules/constructed.py`, pure
functions both. `board_of(item)` reads an entry's zone (`"main"` when absent, so
decks stored before sideboards existed are all main deck).

### Commander and Duel Commander

| Rule | `check_decklist` (names only, on submit) | `check_deck` (fetched cards, in the job) |
|---|---|---|
| Exactly 100 cards, commander included | ✓ | ✓ |
| One commander, or two forming a partner pair | ✓ (count only) | ✓: both `Partner`, `Partner with` each other, both `Friends forever`, or a commander that says "Choose a Background" plus a `Background` (`partner_issue`) |
| Singleton | by name: basic lands and the hand-kept `ANY_NUMBER_CARD_NAMES` are exempt | by card: basic lands and cards whose text says "a deck can have any number of cards named" are exempt; copies are counted per Scryfall id, so both spellings of a double-faced card add up |
| Commander eligibility | — | legendary creature on the front face, or "can be your commander" in its text |
| Color identity | — | every card within the commander's identity |
| Ban list of the format | — | Scryfall `legalities[<format legality_key>]` is `banned` or `not_legal` |

The deck's stored `color_identity` is the union of its commanders' identities, and
`Deck.commander` names both partners as `"A / B"`.

### Constructed formats

| Rule | `check_decklist` | `check_deck` |
|---|---|---|
| Main deck of at least 60 cards | ✓ | ✓ |
| Sideboard of at most 15 cards | ✓ | ✓ |
| No commander | ✓ | ✓ |
| At most 4 copies, main deck and sideboard together | by name, same exemptions as above | by card |
| Restricted cards (Vintage): at most 1 copy | — | `legalities[...] == "restricted"` |
| Ban list of the format | — | `banned` or `not_legal`, sideboard included |

The stored `color_identity` of a constructed deck is the union of its main deck's
identities. `Deck.main_cards`, `sideboard_cards`, `total_cards` (main deck) and
`sideboard_total` split the stored cards; the sideboard is sent to Forge under its
own header, but only the main deck is played.

A missing `legalities` or `color_identity` field (cards cached by older versions)
never fails a deck: legality counts as legal, and color identity falls back to the
mana symbols on the faces.

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

The raw JSON stays in `scryfall_cards`; only this processed copy is on the deck.
`fetch_card_data` gets the raw JSON through `fetch_card_raw(name, cache)` (same key,
same network fallback), which returns it unprocessed. A new field added here reaches newly imported decks only — existing decks need a data
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

`decks/formats.py` is the registry both apps read. Each `Format` has a `family`:

| Key | Family | Life | Seats | Deck | Copies | Commander damage |
|---|---|---|---|---|---|---|
| `commander` | `commander` | 40 | 2–4 | exactly 100 | 1 | 21 |
| `duel` | `commander` | 20 | 2 | exactly 100 | 1 | — |
| `standard`, `pioneer`, `modern`, `legacy`, `vintage`, `pauper` | `constructed` | 20 | 2 | 60+ and ≤15 sideboard | 4 | — |

`DEFAULT_FORMAT = "commander"`. cEDH decks are Commander decks: same rules, same ban
list.

### Adding a format

1. Add an entry to `FORMATS` (or to `CONSTRUCTED_FORMATS` for a 60-card format).
   `legality_key` must be a key of Scryfall's `legalities` object.
2. Generate migrations: `Deck.format` and `Match.format` take their `choices` from
   the registry, so both `decks` and `playtest` get one.
3. Nothing else is needed for a format of an existing family: the import form, the
   legality check, the match form (seat counts) and
   the runner (`GameRules`) all read the registry.
4. A new family needs its own `rules/<family>.py` and a branch in
   `rules/legality.py`.
5. Add tests next to `tests/test_formats.py`, `tests/test_constructed.py` and
   `tests/test_matches_web.py`.

## Metagame decks

`decks/metagame/` imports the top decks of every format as shared, read-only
opponents (`decks.MetagameDeck`, table `metagame_decks`):

| Format | Source | How |
|---|---|---|
| `commander` | EDHTop16 | One GraphQL request: the most popular commanders of the last three months, their meta share, and the full list of their best-placed entry (partners included). |
| constructed | MTGTop8 | The format page's archetypes and shares; the first deck listed for each archetype, through MTGTop8's MTGO export. |
| `duel` | MTGTop8 | The same, from the breakdown the format page loads with a script (`cEDH_decks`, shares in per mille); the export's `Sideboard` (the commander) becomes `Commander`. |

MTGGoldfish is not used: its `robots.txt` disallows `/deck/download*`.

`manage.py sync_metagame [--format <key>] [--limit N]` (`decks/metagame/sync.py`):

- reads the source first — a `SourceError` (network, HTTP error, a page whose layout
  changed) leaves that format's previous snapshot untouched;
- imports every list through `import_decklist`, the same path and rules as user
  decks; a deck that fails is kept as `failed` with its error;
- does not ask the Forge bridge, so the sync runs without it: the decks are stored
  with an empty `coverage` ("not checked"). Starting a playtest sends each
  opponent's deck to the bridge anyway (the first game is created on the spot): a
  deck with a card Forge does not know refuses the session, and what the bridge said
  of a metagame deck becomes its `coverage` (`playtest/runner.py`, `start_game`);
- replaces the format's decks in one transaction. Seats that played a replaced deck
  keep its name (`Seat.metagame_deck` is `SET_NULL`).

The new-playtest form offers the ready metagame decks of the tested deck's format as
opponents, by rank with their share and a badge when Forge does not know a card (the
bridge would refuse the match); the `/metagame` page shows one format's snapshot per
tab (`?format=`). Source parsers are tested on pages
recorded under `tests/fixtures/metagame/`.
