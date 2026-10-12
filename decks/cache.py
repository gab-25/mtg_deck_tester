"""Scryfall cache backends for card JSON.

Both expose ``get_card``/``set_card``, the interface
:func:`decks.scryfall.fetch_card_data` expects. Cards are keyed by a string
like ``card_en_lightning_bolt``. Images are not cached: the browser loads them
straight from Scryfall's CDN.
"""

import json
import os

from django.db import IntegrityError, transaction


class DbCardCache:
    """Cache backed by the ``scryfall_cards`` table."""

    def get_card(self, key: str) -> dict | None:
        from .models import ScryfallCard

        row = ScryfallCard.objects.filter(pk=key).first()
        return row.data if row is not None else None

    def set_card(self, key: str, data: dict) -> None:
        from .models import ScryfallCard

        # Imports run concurrently and may fetch the same card: when another
        # one inserts the key between our lookup and our insert, overwrite it.
        try:
            with transaction.atomic():
                ScryfallCard.objects.update_or_create(key=key, defaults={"data": data})
        except IntegrityError:
            ScryfallCard.objects.filter(pk=key).update(data=data)


class FileCardCache:
    """Filesystem-backed cache, for using the importer without a database."""

    def __init__(self, cache_dir: str):
        self.cards_dir = os.path.join(cache_dir, "cards")
        os.makedirs(self.cards_dir, exist_ok=True)

    def get_card(self, key: str) -> dict | None:
        path = os.path.join(self.cards_dir, f"{key}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None  # Treat a corrupt entry as a miss.

    def set_card(self, key: str, data: dict) -> None:
        path = os.path.join(self.cards_dir, f"{key}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
