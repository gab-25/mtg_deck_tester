"""Queues a Jev benchmark: Jev against Forge's AI with a match's two decks (#79)."""

import random

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from playtest import benchmark
from playtest.agents import typesafe
from playtest.models import Match
from playtest.tasks import run_match_task


class Command(BaseCommand):
    help = "Queue two sessions where Jev plays each deck of a match against Forge's AI, on the same seeds."

    def add_arguments(self, parser):
        parser.add_argument("match_id", help="the match whose two decks the benchmark plays")
        parser.add_argument("--games", type=int, default=20, help="games per deck (default 20)")
        parser.add_argument("--seed", type=int, help="the first seed (default random); reuse it to compare runs")

    def handle(self, *args, match_id, games, seed=None, **options):
        try:
            source = Match.objects.filter(pk=match_id).first()
        except ValidationError as exc:
            raise CommandError(f"No match {match_id}") from exc
        if source is None:
            raise CommandError(f"No match {match_id}")
        if games < 1:
            raise CommandError("--games must be at least 1.")
        if not typesafe.api_key():
            raise CommandError("Jev needs an OpenRouter API key: set OPENROUTER_API_KEY.")
        seed = random.randrange(2**31) if seed is None else seed
        try:
            playtests = benchmark.create_benchmark(source, games, seed)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for match in benchmark.queue_order(playtests):
            run_match_task.delay(str(match.id))
        self.stdout.write(f"Seed {seed}, {games} games per deck, Jev in seat 1 against Forge's AI:")
        for playtest in playtests:
            self.stdout.write(f"  {playtest.id}  Jev plays {playtest.deck_name}")
        self.stdout.write(f"Report: manage.py benchmark_report {' '.join(str(p.id) for p in playtests)}")
