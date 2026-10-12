"""Rebuilds the image URLs of saved decks from the cached card JSON.

Decks used to store ``image_paths``: basenames of images cached in the
database. They now store ``image_urls`` on Scryfall's CDN, taken from the card
JSON in ``scryfall_cards`` (looked up by Scryfall id) rather than built from
the id by hand, since the CDN path layout is not a documented contract. A card
whose JSON is missing ends up with no image.
"""

from django.db import migrations


def _card_image_urls(card: dict) -> list:
    # Frozen copy of decks.scryfall.card_image_urls: migrations must not
    # change behavior when the app code does.
    if "image_uris" in card:
        return [card["image_uris"]["normal"]]
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return [face["image_uris"]["normal"] for face in faces]
    return []


def forwards(apps, schema_editor):
    Deck = apps.get_model("decks", "Deck")
    ScryfallCard = apps.get_model("decks", "ScryfallCard")

    decks = [deck for deck in Deck.objects.all() if deck.cards]
    wanted = {item["data"].get("id") for deck in decks for item in deck.cards}

    urls_by_id = {}
    for row in ScryfallCard.objects.iterator():
        card_id = row.data.get("id")
        if card_id in wanted:
            urls_by_id[card_id] = _card_image_urls(row.data)

    for deck in decks:
        for item in deck.cards:
            data = item["data"]
            data.pop("image_paths", None)
            data["image_urls"] = urls_by_id.get(data.get("id"), [])
        deck.save(update_fields=["cards"])


class Migration(migrations.Migration):

    dependencies = [
        ("decks", "0001_initial"),
    ]

    # Not reversible in any useful way: the cached images are gone. Rolling
    # back leaves the URLs in place and the old code shows no images.
    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
