"""``manage.py card_coverage``: how much of a deck the playtest can simulate."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from decks.cache import DbCardCache
from decks.models import Deck
from decks.rules.decklist import parse_decklist_text
from playtest.engine.coverage import CoverageEntry, CoverageReport, coverage_report
from playtest.judge import compile_deck


class Command(BaseCommand):
    help = "Compiles a deck's cards (through the rules cache) and prints its coverage report."

    def add_arguments(self, parser):
        parser.add_argument("decklist", nargs="?", help="Path to a plain-text decklist file.")
        parser.add_argument("--deck", help="UUID of an imported deck.")

    def handle(self, *args, decklist=None, deck=None, **options):
        if bool(decklist) == bool(deck):
            raise CommandError("Give either a decklist file or --deck <uuid>.")
        entries = _deck_entries(deck) if deck else _file_entries(decklist)
        cards = compile_deck(entries, DbCardCache())
        report = coverage_report(
            [
                CoverageEntry(
                    name=c.card.name,
                    quantity=c.quantity,
                    is_commander=c.is_commander,
                    status=c.card.status,
                    reason_kind=c.card.reason_kind,
                    reason=c.card.reason,
                    is_instant_or_sorcery=c.card.is_instant_or_sorcery,
                )
                for c in cards
            ]
        )
        self.stdout.write(format_report(report))


def _deck_entries(deck_id: str) -> list[dict]:
    try:
        deck = Deck.objects.get(pk=deck_id)
    except (Deck.DoesNotExist, ValidationError) as exc:
        raise CommandError(f"No deck with id {deck_id}.") from exc
    return [
        {
            "name": item["data"]["name"],
            "quantity": item["quantity"],
            "is_commander": item.get("is_commander", False),
        }
        for item in deck.cards
    ]


def _file_entries(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_decklist_text(f.read())
    except OSError as exc:
        raise CommandError(f"Can't read {path}: {exc}") from exc


def format_report(report: CoverageReport) -> str:
    lines = [
        f"Coverage: {report.percent}% of {report.total} cards",
        f"  supported {report.supported}, partial {report.partial}, unsupported {report.unsupported}",
    ]
    if report.commander_unsupported:
        lines.append("WARNING: the commander is unsupported.")
    if report.dead_spells:
        lines.append(
            f"WARNING: {report.dead_spells} dead spells (unsupported instants and sorceries)."
        )
    if report.reasons:
        lines.append("Unsupported by reason:")
        lines += [f"  {kind or 'unknown'}: {count}" for kind, count in report.reasons.items()]
    if report.unsupported_cards:
        lines.append("Unsupported cards:")
        lines += [
            f"  {e.quantity} {e.name} [{e.reason_kind}] {e.reason}"
            for e in report.unsupported_cards
        ]
    return "\n".join(lines) + "\n"
