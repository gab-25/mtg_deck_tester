"""Decks and the shared Scryfall cache."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .formats import DEFAULT_FORMAT, FORMATS, format_choices


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
    # where ``data`` is the processed Scryfall card and its ``image_urls`` point
    # at Scryfall's image CDN.
    cards = models.JSONField(default=list)
    # What the Forge engine made of the deck at its last check:
    # ``{"unresolved": [...], "unsupported_cards": [...]}``; empty until checked.
    coverage = models.JSONField(default=dict, blank=True)
    forge_checked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "decks"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def main_cards(self) -> list:
        """The cards a match plays with (an entry without ``board`` is main deck)."""
        return [item for item in self.cards if item.get("board") != "side"]

    @property
    def sideboard_cards(self) -> list:
        return [item for item in self.cards if item.get("board") == "side"]

    @property
    def total_cards(self) -> int:
        return sum(item["quantity"] for item in self.main_cards)

    @property
    def sideboard_total(self) -> int:
        return sum(item["quantity"] for item in self.sideboard_cards)

    @property
    def has_commander(self) -> bool:
        return FORMATS[self.format].has_commander

    @property
    def is_busy(self) -> bool:
        return self.status in {self.Status.PENDING, self.Status.PROCESSING}

    @property
    def forge_checked(self) -> bool:
        return "unresolved" in (self.coverage or {})

    @property
    def unknown_cards(self) -> list[str]:
        """Cards Forge does not know: a match with this deck would be refused."""
        return list((self.coverage or {}).get("unresolved") or [])

    @property
    def unsupported_cards(self) -> list[str]:
        """Cards Forge knows but cannot play fully."""
        return list((self.coverage or {}).get("unsupported_cards") or [])

    @property
    def forge_ready(self) -> bool:
        return self.forge_checked and not self.unknown_cards


class ScryfallCard(models.Model):
    """Cached Scryfall card JSON, keyed by ``card_en_<slug>``."""

    key = models.CharField(max_length=255, primary_key=True)
    data = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "scryfall_cards"


class BulkDataLoad(models.Model):
    """One load of a Scryfall bulk-data file into ``scryfall_cards``."""

    kind = models.CharField(max_length=64)
    # ``updated_at`` of the remote file; null when loaded from a local file
    # whose metadata could not be read.
    updated_at = models.DateTimeField(null=True, blank=True)
    loaded_at = models.DateTimeField(default=timezone.now)
    inserted = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "scryfall_bulk_loads"


class MetagameDeck(models.Model):
    """A top deck of a format, imported by ``manage.py sync_metagame``.

    Shared and read-only: no owner, replaced as a whole by the next sync of its
    format. Its cards have the same shape as :attr:`Deck.cards`.
    """

    class Source(models.TextChoices):
        EDHTOP16 = "edhtop16", "EDHTop16"
        MTGTOP8 = "mtgtop8", "MTGTop8"

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    format = models.CharField(max_length=16, choices=format_choices())
    source = models.CharField(max_length=16, choices=Source.choices)
    archetype = models.CharField(max_length=255)
    source_url = models.URLField(max_length=500, blank=True, default="")
    # Percent of the format's metagame, when the source gives one.
    meta_share = models.FloatField(null=True, blank=True)
    rank = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(default=timezone.now)
    raw_decklist = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.READY)
    error = models.TextField(blank=True, default="")
    cards = models.JSONField(default=list)
    commander = models.CharField(max_length=255, blank=True, default="")
    color_identity = models.JSONField(default=list)
    # What the Forge engine made of the deck at sync:
    # ``{"unresolved": [...], "unsupported_cards": [...]}``, or empty when the
    # bridge could not be reached.
    coverage = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "metagame_decks"
        ordering = ["format", "rank", "archetype"]

    def __str__(self) -> str:
        return f"{self.archetype} ({self.format})"

    @property
    def main_cards(self) -> list:
        return [item for item in self.cards if item.get("board") != "side"]

    @property
    def total_cards(self) -> int:
        return sum(item["quantity"] for item in self.main_cards)

    @property
    def has_commander(self) -> bool:
        return FORMATS[self.format].has_commander

    @property
    def forge_checked(self) -> bool:
        return "unresolved" in (self.coverage or {})

    @property
    def unknown_cards(self) -> list[str]:
        """Cards Forge does not know: a match with this deck would be refused."""
        return list((self.coverage or {}).get("unresolved") or [])

    @property
    def label(self) -> str:
        """``UR Aggro — 8.4% — 2 cards unknown to Forge``: what the opponent picker shows."""
        parts = [self.archetype]
        if self.meta_share is not None:
            parts.append(f"{self.meta_share:g}%")
        if self.unknown_cards:
            count = len(self.unknown_cards)
            parts.append(f"{count} card{'s' if count > 1 else ''} unknown to Forge")
        return " — ".join(parts)
