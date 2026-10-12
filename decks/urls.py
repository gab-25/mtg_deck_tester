"""Deck routes."""

from django.urls import path

from . import views

urlpatterns = [
    path("decks", views.deck_list, name="deck_list"),
    path("decks/new", views.new_deck, name="new_deck"),
    path("decks/create", views.create_deck, name="create_deck"),
    path("decks/<uuid:deck_id>", views.deck_detail, name="deck_detail"),
    path("decks/<uuid:deck_id>/delete", views.delete_deck, name="delete_deck"),
    path("card-image", views.card_image_modal, name="card_image_modal"),
]
