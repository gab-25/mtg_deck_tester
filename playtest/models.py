"""Matches between AI agents, their seats and what happened in them."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from decks.formats import DEFAULT_FORMAT, FORMATS, format_choices


class Match(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        FINISHED = "finished", "Finished"
        FAILED = "failed", "Failed"

    class EndReason(models.TextChoices):
        LAST_STANDING = "last_standing", "Last player standing"
        TURN_LIMIT = "turn_limit", "Round limit reached"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="matches"
    )
    format = models.CharField(max_length=16, choices=format_choices(), default=DEFAULT_FORMAT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # Seeds the shuffles and the random agents: the same seed and agents replay
    # the same game.
    seed = models.BigIntegerField()
    max_rounds = models.PositiveIntegerField(default=20)
    # The round in progress, updated while the match runs.
    round = models.PositiveIntegerField(default=0)

    winner = models.ForeignKey(
        "Seat", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    end_reason = models.CharField(max_length=16, choices=EndReason.choices, blank=True, default="")
    error = models.TextField(blank=True, default="")
    # The deterministic match summary (playtest.summary.summarize_match),
    # written when the game ends; empty until then.
    summary = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "matches"
        ordering = ["-created_at"]
        verbose_name_plural = "matches"

    @property
    def is_busy(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.RUNNING}

    @property
    def has_commander(self) -> bool:
        return FORMATS[self.format].has_commander


class Seat(models.Model):
    class Agent(models.TextChoices):
        RANDOM = "random", "Random"
        HEURISTIC = "heuristic", "Heuristic"
        JEV = "jev", "Jev"
        LLM = "llm", "LLM"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="seats")
    # 0-based place at the table.
    position = models.PositiveSmallIntegerField()
    # Deleting a deck keeps the matches it played; ``deck_name`` remembers it.
    deck = models.ForeignKey(
        "decks.Deck", null=True, blank=True, on_delete=models.SET_NULL, related_name="seats"
    )
    deck_name = models.CharField(max_length=255)
    # A metagame deck (decks.MetagameDeck) this seat plays; a later sync may
    # delete it, and ``deck_name`` keeps its name.
    metagame_deck = models.ForeignKey(
        "decks.MetagameDeck", null=True, blank=True, on_delete=models.SET_NULL, related_name="seats"
    )
    # The bundled benchmark deck this seat plays (playtest/benchmarks/), or
    # empty for a user's deck.
    benchmark = models.CharField(max_length=32, blank=True, default="")
    agent = models.CharField(max_length=16, choices=Agent.choices, default=Agent.HEURISTIC)
    # OpenRouter model id for an LLM or Jev seat; empty means the default model.
    model = models.CharField(max_length=128, blank=True, default="")
    # The deck's coverage report (playtest.engine.coverage), computed when the
    # match starts: how much of the deck the simulation could play.
    coverage = models.JSONField(default=dict, blank=True)

    # Filled in when the match ends.
    life = models.IntegerField(null=True, blank=True)
    eliminated_round = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "seats"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["match", "position"], name="unique_seat_position"),
        ]

    def __str__(self) -> str:
        return f"Seat {self.position + 1}: {self.deck_name}"

    @property
    def agent_icon(self) -> str:
        """The icon-font ligature for this seat's agent."""
        return {
            self.Agent.RANDOM: "casino",
            self.Agent.HEURISTIC: "rule",
            self.Agent.JEV: "bolt",
            self.Agent.LLM: "psychology",
        }.get(self.agent, "casino")

    @property
    def uses_model(self) -> bool:
        return self.agent in (self.Agent.LLM, self.Agent.JEV)


class MatchEvent(models.Model):
    """One entry of a match log, as the engine reported it."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    round = models.PositiveIntegerField()
    turn = models.PositiveIntegerField()
    # The seat the event is about, or None for table-wide events.
    seat_position = models.PositiveSmallIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=32)
    # The step of the turn it happened in (untap, main1, declare_attackers…).
    step = models.CharField(max_length=32, blank=True, default="")
    text = models.TextField()
    payload = models.JSONField(default=dict)
    reasoning = models.TextField(blank=True, default="")

    class Meta:
        db_table = "match_events"
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["match", "seq"], name="unique_event_seq"),
        ]
