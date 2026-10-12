"""``manage.py build_benchmarks``: regenerate the bundled benchmark decks' rules files."""

import json

from django.core.management.base import BaseCommand, CommandError

from decks.cache import DbCardCache, FileCardCache
from decks.formats import COMMANDER_FAMILY
from decks.rules.decklist import parse_decklist_text
from decks.scryfall import fetch_card_raw
from playtest import benchmarks
from playtest.engine import dsl
from playtest.engine.coverage import coverage_report
from playtest.judge import DeckEntry, compile_card


class Command(BaseCommand):
    help = (
        "Compiles each benchmark decklist (playtest/benchmarks/<key>.txt) and writes its "
        "<key>.rules.json: the card facts and compiled programs a match plays from. "
        "Refuses to write a deck that does not compile at 100% coverage."
    )

    def add_arguments(self, parser):
        parser.add_argument("keys", nargs="*", help="Benchmarks to build (default: all).")
        parser.add_argument(
            "--cache-dir",
            help="Use a filesystem card cache in this directory instead of the database.",
        )

    def handle(self, *args, **options):
        keys = options["keys"] or list(benchmarks.BENCHMARKS)
        cache = FileCardCache(options["cache_dir"]) if options["cache_dir"] else DbCardCache()
        failed = []
        for key in keys:
            if key not in benchmarks.BENCHMARKS:
                raise CommandError(f"Unknown benchmark {key!r}.")
            fmt = "commander" if benchmarks.family(key) == COMMANDER_FAMILY else "modern"
            entries = parse_decklist_text(benchmarks.decklist_path(key).read_text(encoding="utf-8"), fmt)
            cards, coverage = [], []
            for entry in entries:
                raw = fetch_card_raw(entry["name"], cache)
                if raw is None:
                    raise CommandError(f"{key}: {entry['name']} not found on Scryfall.")
                compiled = compile_card(raw, cache)
                coverage.append(DeckEntry(entry["quantity"], entry["is_commander"], compiled).coverage())
                cards.append(
                    {
                        "quantity": entry["quantity"],
                        "is_commander": entry["is_commander"],
                        "card": benchmarks.slim_raw(raw),
                        "status": compiled.status,
                        "program": compiled.program,
                        "reason": compiled.reason,
                        "reason_kind": compiled.reason_kind,
                        "model": compiled.model,
                    }
                )
            report = coverage_report(coverage)
            total = sum(e["quantity"] for e in entries)
            expected = 100 if benchmarks.family(key) == COMMANDER_FAMILY else 60
            if report["percent"] < 100 or total != expected:
                failed.append(key)
                self.stderr.write(f"{key}: {report['percent']}% of {total} cards — not written.")
                for card in report["unsupported_cards"]:
                    self.stderr.write(f"  {card['name']}: {card['reason']}")
                continue
            payload = {"key": key, "name": benchmarks.label(key), "dsl_version": dsl.DSL_VERSION, "cards": cards}
            benchmarks.rules_path(key).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            self.stdout.write(f"{key}: 100% of {total} cards, written to {benchmarks.rules_path(key).name}.")
        if failed:
            raise CommandError(f"Not at 100% coverage: {', '.join(failed)}.")
