"""``manage.py card_coverage``: how much of a deck the playtest can simulate."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from decks.cache import DbCardCache
from decks.models import Deck
from decks.rules.decklist import parse_decklist_text
from playtest.engine.coverage import coverage_report
from playtest.judge import compile_deck


class Command(BaseCommand):
    help = (
        "Compiles a deck's cards (through the compiled-rules cache) and prints its "
        "coverage report: what the playtest can simulate, and why the rest cannot be."
    )

    def add_arguments(self, parser):
        parser.add_argument("decklist", nargs="?", help="A plain-text decklist file.")
        parser.add_argument("--deck", help="The id of an imported deck.")

    def handle(self, *args, **options):
        if bool(options["deck"]) == bool(options["decklist"]):
            raise CommandError("Give either --deck <id> or a decklist file.")
        if options["deck"]:
            deck = Deck.objects.filter(pk=options["deck"]).first()
            if deck is None:
                raise CommandError(f"No deck with id {options['deck']}.")
            title, cards = deck.name, deck.cards
        else:
            path = Path(options["decklist"])
            if not path.is_file():
                raise CommandError(f"No such file: {path}")
            entries = parse_decklist_text(path.read_text(encoding="utf-8"))
            if not entries:
                raise CommandError("No cards could be parsed from the decklist.")
            title = path.name
            cards = [
                {"quantity": e["quantity"], "is_commander": e["is_commander"], "data": {"name": e["name"]}}
                for e in entries
            ]

        compiled = compile_deck(cards, DbCardCache())
        report = coverage_report([entry.coverage() for entry in compiled])
        asked = sum(1 for entry in {e.card.name: e for e in compiled}.values() if entry.card.asked)
        self._print(title, report, asked)

    def _print(self, title: str, report: dict, asked: int) -> None:
        out = self.stdout
        out.write(f"Coverage of {title}: {report['percent']}% of {report['total']} cards")
        out.write(
            f"  supported {report['supported']} · partial {report['partial']} · "
            f"unsupported {report['unsupported']} · judge requests {asked}"
        )
        if report["commander_unsupported"]:
            out.write(self.style.ERROR("  ! The commander is unsupported: it plays as a vanilla card."))
        if report["dead_spells"]:
            out.write(
                self.style.WARNING(f"  ! {report['dead_spells']} dead spell(s): unsupported instants and sorceries.")
            )
        if report["reasons"]:
            out.write("\nUnsupported, by reason:")
            for reason in report["reasons"]:
                out.write(f"  {reason['count']:>3}  {reason['label']}")
            out.write("\nUnsupported cards:")
            for card in report["unsupported_cards"]:
                tags = "".join(
                    f" [{tag}]" for tag, on in (("commander", card["commander"]), ("spell", card["spell"])) if on
                )
                out.write(f"  {card['quantity']}x {card['name']}{tags} — {card['reason']}")
