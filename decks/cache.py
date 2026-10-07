"""Scryfall cache backends: card JSON and image bytes.

Both expose ``get_card``/``set_card``/``has_image``/``get_image``/``set_image``,
the interface :func:`decks.scryfall.fetch_card_data` expects. Cards are keyed
by a string like ``card_en_lightning_bolt`` and images by their basename
(``img_<id>_en.jpg``).

They also keep compiled card rules (``get_rule``/``set_rule``), keyed by oracle
id and DSL version: a rule is a dict with ``program``, ``status``, ``reason``,
``reason_kind`` and ``model``.
"""

import json
import os

from django.db import IntegrityError, transaction
from django.utils import timezone

RULE_FIELDS = ("program", "status", "reason", "reason_kind", "model")


class DbCardCache:
    """Cache backed by the ``scryfall_cards`` / ``scryfall_images`` tables."""

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

    def has_image(self, name: str) -> bool:
        from .models import ScryfallImage

        return ScryfallImage.objects.filter(pk=name).exists()

    def get_image(self, name: str) -> bytes | None:
        from .models import ScryfallImage

        row = ScryfallImage.objects.filter(pk=name).first()
        return bytes(row.data) if row is not None else None

    def set_image(self, name: str, data: bytes) -> None:
        from .models import ScryfallImage

        # Images never change once cached, and concurrent imports may download
        # the same one: whoever stores it first wins, the others are no-ops.
        ScryfallImage.objects.bulk_create(
            [ScryfallImage(name=name, data=data)], ignore_conflicts=True
        )

    def get_rule(self, oracle_id: str, dsl_version: str) -> dict | None:
        from .models import CardRule

        row = CardRule.objects.filter(oracle_id=oracle_id, dsl_version=dsl_version).first()
        return {field: getattr(row, field) for field in RULE_FIELDS} if row is not None else None

    def set_rule(self, oracle_id: str, dsl_version: str, rule: dict) -> None:
        from .models import CardRule

        defaults = {field: rule[field] for field in RULE_FIELDS} | {"compiled_at": timezone.now()}
        # Two matches can compile the same card at once: when the other one
        # inserts it between our lookup and our insert, overwrite it.
        try:
            with transaction.atomic():
                CardRule.objects.update_or_create(
                    oracle_id=oracle_id, dsl_version=dsl_version, defaults=defaults
                )
        except IntegrityError:
            CardRule.objects.filter(oracle_id=oracle_id, dsl_version=dsl_version).update(**defaults)


class FileCardCache:
    """Filesystem-backed cache, for using the importer without a database."""

    def __init__(self, cache_dir: str):
        self.cards_dir = os.path.join(cache_dir, "cards")
        self.images_dir = os.path.join(cache_dir, "images")
        self.rules_dir = os.path.join(cache_dir, "rules")
        os.makedirs(self.cards_dir, exist_ok=True)
        os.makedirs(self.images_dir, exist_ok=True)
        os.makedirs(self.rules_dir, exist_ok=True)

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

    def has_image(self, name: str) -> bool:
        return os.path.exists(os.path.join(self.images_dir, name))

    def get_image(self, name: str) -> bytes | None:
        path = os.path.join(self.images_dir, name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return None

    def set_image(self, name: str, data: bytes) -> None:
        try:
            with open(os.path.join(self.images_dir, name), "wb") as f:
                f.write(data)
        except Exception:
            pass

    def get_rule(self, oracle_id: str, dsl_version: str) -> dict | None:
        path = os.path.join(self.rules_dir, f"{oracle_id}_v{dsl_version}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None  # Treat a corrupt entry as a miss.

    def set_rule(self, oracle_id: str, dsl_version: str, rule: dict) -> None:
        path = os.path.join(self.rules_dir, f"{oracle_id}_v{dsl_version}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {field: rule[field] for field in RULE_FIELDS}, f, ensure_ascii=False, indent=2
                )
        except Exception:
            pass
