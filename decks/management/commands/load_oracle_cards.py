"""``manage.py load_oracle_cards``: fill the card cache from Scryfall bulk data."""

from django.core.management.base import BaseCommand

from decks import oracle_bulk


class Command(BaseCommand):
    help = "Loads Scryfall's Oracle Cards bulk file into the scryfall_cards cache."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", help="Load an already downloaded bulk file instead of fetching it."
        )
        parser.add_argument(
            "--if-newer",
            action="store_true",
            help="Skip the download when the last loaded file is not older than the remote one.",
        )

    def handle(self, *args, file=None, if_newer=False, **options):
        if file:
            with open(file, "rb") as f:
                counts = oracle_bulk.load_file(f)
            updated_at = None
        else:
            counts, updated_at = oracle_bulk.load_remote(if_newer=if_newer)
            if counts is None:
                self.stdout.write(f"Oracle Cards already up to date ({updated_at}).")
                return

        self.stdout.write(
            self.style.SUCCESS(
                f"Oracle Cards loaded: {counts.inserted} inserted, "
                f"{counts.updated} updated, {counts.skipped} skipped "
                f"(file updated_at: {updated_at or 'unknown'})."
            )
        )
