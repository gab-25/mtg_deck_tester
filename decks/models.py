"""Decks and the shared Scryfall cache."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .formats import DEFAULT_FORMAT, format_choices


class Deck(models.Model):
    """A Commander decklist imported from text, with its cards fetched from Scryfall."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Importing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    # UUID primary key so deck URLs aren't sequentially enumerable.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="decks"
    )
    name = models.CharField(max_length=255)
    format = models.CharField(max_length=16, choices=format_choices(), default=DEFAULT_FORMAT)
    raw_decklist = models.TextField()

    # Lifecycle of the background import. Defaults to READY so decks created
    # directly (tests, fixtures) need no extra handling; the creation view sets
    # PENDING explicitly and the import job advances it.
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.READY)
    # The failure reason when ``status`` is FAILED.
    error = models.TextField(blank=True, default="")

    commander = models.CharField(max_length=255, blank=True, default="")
    # WUBRG letters of the deck's color identity, e.g. ``["W", "U"]``.
    color_identity = models.JSONField(default=list)
    # Fetched cards: ``[{"quantity": int, "is_commander": bool, "data": {...}}]``,
    # where ``data`` is the processed Scryfall card and its ``image_paths`` are
    # cache keys (image basenames).
    cards = models.JSONField(default=list)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "decks"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def total_cards(self) -> int:
        return sum(item["quantity"] for item in self.cards)

    @property
    def is_busy(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.PROCESSING}


class ScryfallCard(models.Model):
    """Cached Scryfall card JSON, keyed by ``card_en_<slug>``."""

    key = models.CharField(max_length=255, primary_key=True)
    data = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "scryfall_cards"


class ScryfallImage(models.Model):
    """Cached card image bytes, keyed by basename (``img_<id>_en.jpg``)."""

    name = models.CharField(max_length=255, primary_key=True)
    data = models.BinaryField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "scryfall_images"


class CardRule(models.Model):
    """A card's compiled effect program, keyed by oracle id and DSL version.

    ``decks`` stores the program as opaque JSON and never imports the DSL: the
    playtest owns its meaning. ``model`` and ``compiled_at`` are not part of the
    key, so a new model release doesn't recompile the whole pool.
    """

    class Status(models.TextChoices):
        SUPPORTED = "supported", "Supported"
        PARTIAL = "partial", "Partial"
        UNSUPPORTED = "unsupported", "Unsupported"

    oracle_id = models.CharField(max_length=64)
    dsl_version = models.CharField(max_length=16)
    # The validated program, or null when the card is unsupported.
    program = models.JSONField(null=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    reason = models.TextField(blank=True, default="")
    # Why it is unsupported, as a category the coverage report groups by.
    reason_kind = models.CharField(max_length=32, blank=True, default="")
    # The versioned model that answered (empty when no model was asked).
    model = models.CharField(max_length=64, blank=True, default="")
    compiled_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "card_rules"
        constraints = [
            models.UniqueConstraint(
                fields=["oracle_id", "dsl_version"], name="card_rule_per_dsl_version"
            ),
        ]
