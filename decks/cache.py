"""Scryfall cache backends for card JSON.

Both expose ``get_card``/``set_card``, the interface
:func:`decks.scryfall.fetch_card_data` expects. Cards are keyed by a string
like ``card_en_lightning_bolt``. Images are not cached: the browser loads them
straight from Scryfall's CDN.

They also keep the playtest judge's compiled card rules (``get_rule`` /
``set_rule``), keyed by oracle id and DSL version: the codebase threads exactly
one cache object, so it carries both.
"""

import json
import os

from django.db import IntegrityError, transaction
from django.utils import timezone


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


    def get_rule(self, oracle_id: str, dsl_version: str) -> dict | None:
        from .models import CardRule

        row = CardRule.objects.filter(oracle_id=oracle_id, dsl_version=dsl_version).first()
        if row is None:
            return None
        return {
            "program": row.program,
            "status": row.status,
            "reason": row.reason,
            "reason_kind": row.reason_kind,
            "model": row.model,
        }

    def get_rules(self, oracle_ids, dsl_version: str) -> dict[str, dict]:
        """Every cached rule among ``oracle_ids``, by oracle id (one query)."""
        from .models import CardRule

        rows = CardRule.objects.filter(oracle_id__in=list(oracle_ids), dsl_version=dsl_version)
        return {
            row.oracle_id: {"status": row.status, "reason": row.reason, "reason_kind": row.reason_kind}
            for row in rows
        }

    def set_rule(self, oracle_id: str, dsl_version: str, **fields) -> None:
        from .models import CardRule

        fields = {**fields, "compiled_at": timezone.now()}
        # Two matches may compile the same card at once: when another one
        # inserts the key between our lookup and our insert, overwrite it.
        try:
            with transaction.atomic():
                CardRule.objects.update_or_create(
                    oracle_id=oracle_id, dsl_version=dsl_version, defaults=fields
                )
        except IntegrityError:
            CardRule.objects.filter(oracle_id=oracle_id, dsl_version=dsl_version).update(**fields)


class FileCardCache:
    """Filesystem-backed cache, for using the importer without a database."""

    def __init__(self, cache_dir: str):
        self.cards_dir = os.path.join(cache_dir, "cards")
        self.rules_dir = os.path.join(cache_dir, "rules")
        os.makedirs(self.cards_dir, exist_ok=True)
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

    def _rule_path(self, oracle_id: str, dsl_version: str) -> str:
        safe = "".join(c if c.isalnum() or c == "-" else "_" for c in oracle_id)
        return os.path.join(self.rules_dir, f"{safe}_v{dsl_version}.json")

    def get_rule(self, oracle_id: str, dsl_version: str) -> dict | None:
        path = self._rule_path(oracle_id, dsl_version)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None  # Treat a corrupt entry as a miss.

    def set_rule(self, oracle_id: str, dsl_version: str, **fields) -> None:
        try:
            with open(self._rule_path(oracle_id, dsl_version), "w", encoding="utf-8") as f:
                json.dump(fields, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
