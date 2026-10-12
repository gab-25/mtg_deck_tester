"""Django admin registrations."""

from django.contrib import admin

from .models import Deck, ScryfallCard


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
