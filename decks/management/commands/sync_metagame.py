"""``manage.py sync_metagame``: import the top decks of each format."""

from django.core.management.base import BaseCommand, CommandError

from decks.cache import DbCardCache
from decks.formats import FORMATS
from decks.metagame import SourceError, sync


class Command(BaseCommand):
    help = (
        "Fetches the current top decks of each format (EDHTop16 for Commander, MTGTop8 "
        "for Duel Commander and the constructed formats), imports them, checks their cards "
        "against the Forge bridge when it runs, and replaces that format's metagame decks."
    )

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=list(FORMATS), help="Only this format (default: all).")
        parser.add_argument("--limit", type=int, default=10, help="Decks per format (default: 10).")

    def handle(self, *args, **options):
        formats = [options["format"]] if options["format"] else list(FORMATS)
        cache = DbCardCache()
        failed = []
        for fmt in formats:
            try:
                report = sync.sync_format(fmt, options["limit"], cache)
            except SourceError as exc:
                failed.append(fmt)
                self.stderr.write(f"{fmt}: {exc}")
                continue
            if report.unknown_to_forge is None:
                forge = "not checked (Forge bridge unreachable)"
            else:
                forge = f"{report.unknown_to_forge} with cards Forge does not know"
            self.stdout.write(f"{fmt}: {report.imported} imported, {report.failed} failed, {forge}")
        if failed:
            raise CommandError(f"Could not sync: {', '.join(failed)}. Their previous decks are kept.")
