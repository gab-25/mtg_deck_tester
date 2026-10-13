"""Data migrations on saved decks."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = ("decks", "0001_initial")
AFTER = ("decks", "0002_card_image_urls")
CDN = "https://cards.scryfall.io/normal"


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


@pytest.mark.django_db(transaction=True)
def test_saved_decks_get_their_image_urls_from_the_cached_card_json(django_user_model):
    apps = _migrate(BEFORE)
    try:
        owner = django_user_model.objects.create_user(username="old")
        ScryfallCard = apps.get_model("decks", "ScryfallCard")
        Deck = apps.get_model("decks", "Deck")
        ScryfallCard.objects.create(
            key="card_en_forest",
            data={"id": "f1", "name": "Forest", "image_uris": {"normal": f"{CDN}/front/f1.jpg"}},
        )
        ScryfallCard.objects.create(
            key="card_en_delver_of_secrets",
            data={
                "id": "d1",
                "name": "Delver of Secrets // Insectile Aberration",
                "card_faces": [
                    {"image_uris": {"normal": f"{CDN}/front/d1.jpg"}},
                    {"image_uris": {"normal": f"{CDN}/back/d1.jpg"}},
                ],
            },
        )
        ScryfallCard.objects.create(key="card_en_nope", data={"error": "not_found"})
        Deck.objects.create(
            owner_id=owner.id,
            name="Old",
            raw_decklist="",
            cards=[
                {"quantity": 1, "data": {"id": "f1", "image_paths": ["img_f1_en.jpg"]}},
                {
                    "quantity": 1,
                    "data": {
                        "id": "d1",
                        "image_paths": ["img_d1_en_face0.jpg", "img_d1_en_face1.jpg"],
                    },
                },
                # Its card JSON is gone: it ends up with no image, not a broken page.
                {"quantity": 1, "data": {"id": "gone", "image_paths": ["img_gone_en.jpg"]}},
            ],
        )

        apps = _migrate(AFTER)
        cards = [item["data"] for item in apps.get_model("decks", "Deck").objects.get().cards]
        assert [card.get("image_urls") for card in cards] == [
            [f"{CDN}/front/f1.jpg"],
            [f"{CDN}/front/d1.jpg", f"{CDN}/back/d1.jpg"],
            [],
        ]
        assert all("image_paths" not in card for card in cards)
    finally:
        # Back to the latest schema of every app: unapplying decks unapplied
        # the playtest migrations that depend on it too.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_heuristic_seats_become_forge_ai_seats(django_user_model):
    apps = _migrate(("playtest", "0006_forge_matches"))
    try:
        owner = django_user_model.objects.create_user(username="old")
        Match = apps.get_model("playtest", "Match")
        Seat = apps.get_model("playtest", "Seat")
        match = Match.objects.create(owner_id=owner.id, seed=1)
        Seat.objects.create(match=match, position=0, deck_name="A", agent="heuristic")
        Seat.objects.create(match=match, position=1, deck_name="B", agent="random")
        apps = _migrate(("playtest", "0007_heuristic_seats_to_forge_ai"))
        agents = list(apps.get_model("playtest", "Seat").objects.order_by("position").values_list("agent", flat=True))
        assert agents == ["forge_ai", "random"]
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
