"""Jev's numbers over the finished matches of benchmark sessions, side by side (#79)."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from playtest import benchmark
from playtest.models import Playtest


class Command(BaseCommand):
    help = "Report Jev's record, confidence, flags and agreement with Forge's AI over playtest sessions."

    def add_arguments(self, parser):
        parser.add_argument("playtest_ids", nargs="+", help="sessions, one column each, plus a total")

    def handle(self, *args, playtest_ids, **options):
        playtests = []
        for playtest_id in playtest_ids:
            try:
                playtest = Playtest.objects.filter(pk=playtest_id).first()
            except ValidationError as exc:
                raise CommandError(f"No playtest {playtest_id}") from exc
            if playtest is None:
                raise CommandError(f"No playtest {playtest_id}")
            playtests.append(playtest)
        matches = {p.id: list(p.matches.select_related("winner")) for p in playtests}
        reports = [benchmark.report(f"{p.deck_name} ({str(p.id)[:8]})", matches[p.id]) for p in playtests]
        if len(playtests) > 1:
            reports.append(benchmark.report("All", [m for p in playtests for m in matches[p.id]]))
        self.stdout.write(benchmark.table(reports), ending="")
