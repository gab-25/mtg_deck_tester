"""Django admin registrations."""

from django.contrib import admin

from .models import Deck, MetagameDeck, ScryfallCard


@admin.register(Deck)
class DeckAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "format", "commander", "status", "created_at")
    list_filter = ("status", "format")
    search_fields = ("name", "commander", "raw_decklist")
    readonly_fields = ("created_at",)


@admin.register(ScryfallCard)
class ScryfallCardAdmin(admin.ModelAdmin):
    list_display = ("key", "created_at")
    search_fields = ("key",)
    readonly_fields = ("created_at",)


@admin.register(MetagameDeck)
class MetagameDeckAdmin(admin.ModelAdmin):
    list_display = ("format", "rank", "archetype", "source", "meta_share", "status", "fetched_at")
    list_filter = ("format", "source", "status")
    search_fields = ("archetype", "commander")
    readonly_fields = ("fetched_at",)
