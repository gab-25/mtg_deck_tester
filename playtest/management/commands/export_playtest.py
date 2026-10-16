"""Writes every match of a playtest session, one file each."""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from playtest import export
from playtest.models import Playtest


class Command(BaseCommand):
    help = "Export every match of a playtest into --output, one file per match."

    def add_arguments(self, parser):
        parser.add_argument("playtest_id")
        parser.add_argument("--format", choices=sorted(export.FORMATS), default="jsonl")
        parser.add_argument("--output", required=True, help="the directory to write into")

    def handle(self, *args, playtest_id, format, output, **options):
        try:
            playtest = Playtest.objects.filter(pk=playtest_id).first()
        except ValidationError as exc:
            raise CommandError(f"No playtest {playtest_id}") from exc
        if playtest is None:
            raise CommandError(f"No playtest {playtest_id}")
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        for match in playtest.matches.order_by("seed"):
            path = folder / export.file_name(match, format)
            path.write_text(export.FORMATS[format](match))
            self.stdout.write(str(path))
