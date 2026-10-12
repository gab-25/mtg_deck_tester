"""Django admin registrations."""

from django.contrib import admin

from .models import CardRule, Deck, MetagameDeck, ScryfallCard


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


@admin.register(CardRule)
class CardRuleAdmin(admin.ModelAdmin):
    list_display = ("oracle_id", "dsl_version", "status", "reason_kind", "model", "compiled_at")
    list_filter = ("status", "reason_kind", "dsl_version")
    search_fields = ("oracle_id", "reason")
    readonly_fields = ("compiled_at",)


@admin.register(MetagameDeck)
class MetagameDeckAdmin(admin.ModelAdmin):
    list_display = ("format", "rank", "archetype", "source", "meta_share", "status", "fetched_at")
    list_filter = ("format", "source", "status")
    search_fields = ("archetype", "commander")
    readonly_fields = ("fetched_at",)
