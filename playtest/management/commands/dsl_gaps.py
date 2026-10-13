"""``manage.py dsl_gaps``: what keeps the metagame's cards out of the DSL, ranked."""

from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from decks.cache import DbCardCache
from decks.formats import FORMATS
from decks.models import MetagameDeck
from decks.rules.decklist import parse_decklist_text
from decks.scryfall import fetch_card_raw
from playtest.engine.cardrules import characteristics
from playtest.engine.gaps import card_gaps


class Command(BaseCommand):
    help = (
        "Lists every reason the cards of the metagame decks (or of decklist files) cannot be "
        "compiled, weighted by how many deck slots each blocks, and the cards one reason "
        "alone keeps out. Reads cards through the card cache (Scryfall on a miss); never asks the judge."
    )

    def add_arguments(self, parser):
        parser.add_argument("decklists", nargs="*", help="Decklist files instead of the metagame decks.")
        parser.add_argument("--format", choices=list(FORMATS), help="Only this format's metagame decks.")
        parser.add_argument("--top", type=int, default=40, help="How many reasons to list (default 40).")

    def _decks(self, options):
        if options["decklists"]:
            fmt = options["format"] or "modern"
            for path in options["decklists"]:
                text = Path(path).read_text(encoding="utf-8")
                entries = parse_decklist_text(text, fmt)
                yield fmt, [(e["quantity"], e["name"]) for e in entries if e.get("board") != "side"]
            return
        decks = MetagameDeck.objects.filter(status=MetagameDeck.Status.READY)
        if options["format"]:
            decks = decks.filter(format=options["format"])
        for deck in decks:
            yield deck.format, [(item["quantity"], item["data"]["name"]) for item in deck.main_cards]

    def handle(self, *args, **options):
        cache = DbCardCache()
        memo: dict[str, tuple[list[str], bool] | None] = {}
        slots = Counter()
        sole = Counter()
        examples: dict[str, Counter] = defaultdict(Counter)
        per_format: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        for fmt, cards in self._decks(options):
            for quantity, name in cards:
                if name not in memo:
                    raw = fetch_card_raw(name, cache)
                    memo[name] = None if raw is None else (card_gaps(raw), characteristics(raw).is_land)
                if memo[name] is None:
                    continue
                gaps, is_land = memo[name]
                row = per_format[fmt]
                row[0] += quantity
                row[1] += quantity * (not gaps)
                if not is_land:
                    row[2] += quantity
                    row[3] += quantity * (not gaps)
                    for gap in gaps:
                        slots[gap] += quantity
                        examples[gap][name] += quantity
                    if len(gaps) == 1:
                        sole[gaps[0]] += quantity
        if not per_format:
            raise CommandError("No decks to measure: run sync_metagame first, or pass decklist files.")

        out = self.stdout
        out.write("Compilable without a judge refusal (grammar coverage):")
        nonland = sum(r[2] for r in per_format.values())
        for fmt, (total, ok, nl, nl_ok) in sorted(per_format.items()):
            out.write(f"  {fmt:10} all {ok * 100 // max(total, 1):3d}%   non-land {nl_ok * 100 // max(nl, 1):3d}%  ({nl_ok}/{nl})")
        out.write(f"\nReasons by non-land slots blocked (of {nonland}):")
        for gap, count in slots.most_common(options["top"]):
            names = ", ".join(n for n, _ in examples[gap].most_common(3))
            out.write(f"  {count:5d} {count * 100 / nonland:5.1f}%  {gap[:90]}  — {names}")
        out.write("\nThe only reason a card is out (slots unblocked by fixing it alone):")
        for gap, count in sole.most_common(options["top"]):
            out.write(f"  {count:5d}  {gap[:90]}")
