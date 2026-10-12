"""Tests for Scryfall data-processing helpers (no network access)."""

from decks.scryfall import (
    _extract_price_eur,
    card_image_urls,
    get_face_details,
    process_cached_card,
)

CDN = "https://cards.scryfall.io/normal"


class TestExtractPriceEur:
    def test_uses_eur(self):
        assert _extract_price_eur({"prices": {"eur": "12.50"}}) == 12.50

    def test_falls_back_to_eur_foil(self):
        assert _extract_price_eur({"prices": {"eur_foil": "30.0"}}) == 30.0

    def test_converts_usd_when_no_eur(self):
        assert _extract_price_eur({"prices": {"usd": "10.00"}}) == 10.0 * 0.93

    def test_converts_usd_foil_when_only_foil_usd(self):
        result = _extract_price_eur({"prices": {"usd_foil": "10.00"}})
        assert result == 10.0 * 0.93

    def test_missing_prices_returns_zero(self):
        assert _extract_price_eur({}) == 0.0

    def test_invalid_eur_falls_through(self):
        assert _extract_price_eur({"prices": {"eur": "n/a", "usd": "5.00"}}) == 5.0 * 0.93

    def test_eur_preferred_over_usd(self):
        prices = {"prices": {"eur": "1.00", "usd": "100.00"}}
        assert _extract_price_eur(prices) == 1.00


class TestGetFaceDetails:
    def test_reads_english_fields(self):
        face = {
            "name": "Lightning Bolt",
            "type_line": "Instant",
            "oracle_text": "Deal 3 damage",
            "mana_cost": "{R}",
        }
        details = get_face_details(face)
        assert details == {
            "name": "Lightning Bolt",
            "mana_cost": "{R}",
            "type_line": "Instant",
            "rules_text": "Deal 3 damage",
        }

    def test_missing_fields_default_empty(self):
        face = {
            "name": "Lightning Bolt",
            "type_line": "Instant",
            "oracle_text": "Deal 3 damage",
        }
        details = get_face_details(face)
        assert details["name"] == "Lightning Bolt"
        assert details["type_line"] == "Instant"
        assert details["rules_text"] == "Deal 3 damage"
        assert details["mana_cost"] == ""

    def test_strips_whitespace(self):
        details = get_face_details({"name": "  Bolt  ", "mana_cost": " {R} "})
        assert details["name"] == "Bolt"
        assert details["mana_cost"] == "{R}"

    def test_empty_input(self):
        details = get_face_details({})
        assert details == {
            "name": "",
            "mana_cost": "",
            "type_line": "",
            "rules_text": "",
        }


class TestProcessCachedCard:
    def test_uses_english_name(self):
        out = process_cached_card({"id": "x", "name": "Forest"})
        assert out["name"] == "Forest"

    def test_missing_name_falls_back(self):
        out = process_cached_card({"id": "x"})
        assert out["name"] == "Unknown Card"

    def test_carries_the_legalities_through(self):
        card = {"id": "x", "name": "Black Lotus", "legalities": {"commander": "banned"}}
        out = process_cached_card(card)
        assert out["legalities"] == {"commander": "banned"}

    def test_missing_legalities_becomes_an_empty_dict(self):
        out = process_cached_card({"id": "x", "name": "Forest"})
        assert out["legalities"] == {}


class TestProducedMana:
    """``produced_mana`` drives the deck's colored-source count."""

    def test_produced_mana_is_carried_through(self):
        card = {
            "id": "x",
            "name": "Command Tower",
            "type_line": "Land",
            "produced_mana": ["W", "U", "B", "R", "G"],
        }
        processed = process_cached_card(card)
        assert processed["produced_mana"] == ["W", "U", "B", "R", "G"]

    def test_missing_produced_mana_is_an_empty_list(self):
        card = {"id": "x", "name": "Lightning Bolt", "type_line": "Instant"}
        assert process_cached_card(card)["produced_mana"] == []


class TestCardImageUrls:
    """Images are served from Scryfall's CDN: the import only keeps their URLs."""

    def test_single_faced_card_has_one_url(self):
        card = {"id": "x", "image_uris": {"normal": f"{CDN}/front/x.jpg?1"}}
        assert card_image_urls(card) == [f"{CDN}/front/x.jpg?1"]

    def test_double_faced_card_has_one_url_per_face(self):
        card = {
            "id": "x",
            "card_faces": [
                {"name": "Front", "image_uris": {"normal": f"{CDN}/front/x.jpg"}},
                {"name": "Back", "image_uris": {"normal": f"{CDN}/back/x.jpg"}},
            ],
        }
        assert card_image_urls(card) == [f"{CDN}/front/x.jpg", f"{CDN}/back/x.jpg"]

    def test_split_card_uses_the_shared_root_image(self):
        card = {
            "id": "x",
            "image_uris": {"normal": f"{CDN}/front/x.jpg"},
            "card_faces": [{"name": "Fire"}, {"name": "Ice"}],
        }
        assert card_image_urls(card) == [f"{CDN}/front/x.jpg"]

    def test_card_without_images_has_none(self):
        assert card_image_urls({"id": "x"}) == []

    def test_processed_card_carries_the_urls(self):
        card = {"id": "x", "name": "Forest", "image_uris": {"normal": f"{CDN}/front/x.jpg"}}
        assert process_cached_card(card)["image_urls"] == [f"{CDN}/front/x.jpg"]
