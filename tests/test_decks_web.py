"""Web tests for the deck pages and the import job (hermetic, no network)."""

import pytest

from decks import importer
from decks.models import Deck

from .factories import legal_decklist, make_deck, scryfall_card


def _fake_fetch(name, cache):
    """Resolves any name to a green card; the commander is a legendary creature."""
    if name == "Missing Card":
        return None
    if name.startswith("Atraxa"):
        return scryfall_card(name, "Legendary Creature — Phyrexian Angel", 4.0, "4", "4")
    if name == "Forest":
        return scryfall_card(name, "Basic Land — Forest")
    if name == "Mountain":
        return scryfall_card(name, "Basic Land — Mountain")
    if name.startswith("Spell ") or name.startswith("Side "):
        card = scryfall_card(name, "Instant", 1.0)
        card["legalities"]["modern"] = "legal"
        return card
    return scryfall_card(name, "Creature — Bear", 2.0, "2", "2")


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="owner", password="pw")


@pytest.fixture
def client(client, user, monkeypatch):
    monkeypatch.setattr(importer, "fetch_card_data", _fake_fetch)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_every_page_requires_login(client):
    client.logout()
    for url in ["/", "/decks", "/decks/new", "/matches", "/matches/new"]:
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith("/login")


@pytest.mark.django_db
def test_home_lists_recent_decks_and_matches(client, user):
    make_deck(user, name="Green Bears")
    response = client.get("/")
    assert response.status_code == 200
    assert b"Green Bears" in response.content
    assert b"MTG Deck Tester" in response.content


@pytest.mark.django_db
def test_importing_a_legal_deck(client, user):
    response = client.post(
        "/decks/create", {"name": "Atraxa", "format": "commander", "decklist": legal_decklist()}
    )
    assert response.status_code == 302
    deck = Deck.objects.get(owner=user)
    assert deck.status == Deck.Status.READY
    assert deck.commander == "Atraxa, Praetors' Voice"
    assert deck.total_cards == 100


@pytest.mark.django_db
def test_an_illegal_decklist_is_rejected_inline(client):
    response = client.post("/decks/create", {"name": "Short", "decklist": "1 Sol Ring"})
    assert response.status_code == 422
    assert b"Commander requires exactly" in response.content
    assert not Deck.objects.exists()


@pytest.mark.django_db
def test_an_unknown_card_fails_the_import(client, user):
    decklist = legal_decklist().replace("1 Spell 0", "1 Missing Card")
    client.post("/decks/create", {"name": "Typo", "decklist": decklist})
    deck = Deck.objects.get(owner=user)
    assert deck.status == Deck.Status.FAILED
    assert "Missing Card" in deck.error
    page = client.get(f"/decks/{deck.id}")
    assert b"Missing Card" in page.content


@pytest.mark.django_db
def test_a_banned_card_fails_the_import_for_its_format(client, user, monkeypatch):
    def fetch(name, cache):
        card = _fake_fetch(name, cache)
        if name == "Spell 1":
            card["legalities"] = {"commander": "legal", "duel": "banned"}
        return card

    monkeypatch.setattr(importer, "fetch_card_data", fetch)
    client.post("/decks/create", {"name": "Duel", "format": "duel", "decklist": legal_decklist()})
    deck = Deck.objects.get(owner=user)
    assert deck.status == Deck.Status.FAILED
    assert "banned in Duel Commander" in deck.error


@pytest.mark.django_db
def test_deck_detail_groups_the_cards(client, user):
    deck = make_deck(user)
    response = client.get(f"/decks/{deck.id}")
    assert response.status_code == 200
    assert b"Creatures" in response.content
    assert b"Lands" in response.content
    assert b"Play a match" in response.content


@pytest.mark.django_db
def test_someone_elses_deck_is_a_404(client, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    deck = make_deck(other)
    assert client.get(f"/decks/{deck.id}").status_code == 404
    assert client.post(f"/decks/{deck.id}/delete").status_code == 404
    assert Deck.objects.filter(pk=deck.id).exists()


@pytest.mark.django_db
def test_deck_list_shows_only_my_decks(client, user, django_user_model):
    make_deck(user, name="Mine")
    make_deck(django_user_model.objects.create_user(username="other"), name="Theirs")
    content = client.get("/decks").content
    assert b"Mine" in content
    assert b"Theirs" not in content


@pytest.mark.django_db
def test_deleting_a_deck(client, user):
    deck = make_deck(user)
    assert client.post(f"/decks/{deck.id}/delete").status_code == 302
    assert not Deck.objects.exists()


FRONT = "https://cards.scryfall.io/normal/front/a/b/ab.jpg?1"
BACK = "https://cards.scryfall.io/normal/back/a/b/ab.jpg?1"


@pytest.mark.django_db
def test_deck_page_loads_images_from_scryfall_cdn(client, user):
    cards = [
        {
            "quantity": 1,
            "is_commander": True,
            "data": scryfall_card(
                "Bear Lord", "Legendary Creature — Bear", 4.0, "5", "5", image_urls=[FRONT, BACK]
            ),
        }
    ]
    deck = make_deck(user, cards=cards)
    content = client.get(f"/decks/{deck.id}").content.decode()
    assert f'<img src="{FRONT}"' in content
    # The thumbnail's zoom request carries both faces, for the flip.
    assert "url=https%3A%2F%2Fcards.scryfall.io%2Fnormal%2Ffront" in content
    assert "url=https%3A%2F%2Fcards.scryfall.io%2Fnormal%2Fback" in content


@pytest.mark.django_db
def test_card_image_modal_shows_every_face(client):
    response = client.get("/card-image", {"url": [FRONT, BACK]})
    assert response.status_code == 200
    content = response.content.decode()
    assert FRONT in content and BACK in content
    assert "card-face-flip" in content


@pytest.mark.django_db
def test_card_image_modal_without_urls_is_empty(client):
    response = client.get("/card-image")
    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x.jpg",
        "http://cards.scryfall.io/normal/front/x.jpg",
        "https://cards.scryfall.io.evil.example/x.jpg",
        "javascript:alert(1)",
    ],
)
def test_card_image_modal_refuses_urls_outside_scryfall_cdn(client, url):
    assert client.get("/card-image", {"url": [FRONT, url]}).status_code == 400


@pytest.mark.django_db
def test_images_are_no_longer_proxied(client):
    assert client.get("/media/img_x_en.jpg").status_code == 404


@pytest.mark.django_db
def test_importing_a_constructed_deck_keeps_the_sideboard(client, user):
    from .factories import legal_constructed_decklist

    response = client.post("/decks/create", {"name": "Burn", "format": "modern", "decklist": legal_constructed_decklist()})
    assert response.status_code == 302
    deck = Deck.objects.get(owner=user)
    assert deck.status == Deck.Status.READY, deck.error
    assert deck.commander == ""
    assert (deck.total_cards, deck.sideboard_total) == (60, 15)
    page = client.get(f"/decks/{deck.id}").content.decode()
    assert "Sideboard" in page and ">Commander</div>" not in page


@pytest.mark.django_db
def test_an_illegal_constructed_deck_is_refused_on_the_spot(client, user):
    response = client.post("/decks/create", {"name": "Short", "format": "modern", "decklist": "4 Spell\n20 Mountain\n"})
    assert response.status_code == 422
    assert b"needs at least 60" in response.content


@pytest.mark.django_db
def test_an_old_deck_without_boards_is_all_main(client, user):
    deck = make_deck(user)
    assert deck.total_cards == 100 and deck.sideboard_total == 0
    assert client.get(f"/decks/{deck.id}").status_code == 200


@pytest.mark.django_db
def test_the_import_form_shows_the_rules_of_each_format(client):
    page = client.get("/decks/new").content.decode()
    assert 'data-format-rules="commander"' in page and 'data-format-rules="modern"' in page
    assert "at most 15" in page


@pytest.mark.django_db
def test_home_names_the_competitive_formats(client):
    page = client.get("/").content.decode()
    assert "Modern" in page and "Pauper" in page


@pytest.mark.django_db
def test_the_metagame_page_lists_each_format(client):
    from decks.models import MetagameDeck

    MetagameDeck.objects.create(
        format="modern", source="mtgtop8", archetype="UR Aggro", source_url="https://www.mtgtop8.com/archetype?a=1",
        meta_share=8.0, rank=1, coverage={"percent": 46},
    )
    MetagameDeck.objects.create(format="modern", source="mtgtop8", archetype="Broken", rank=2, status="failed", error="Missing Card")
    page = client.get("/metagame").content.decode()
    assert "UR Aggro" in page and "8%" in page and "46%" in page
    assert "Missing Card" in page
    assert "Not synced yet" in page  # every other format
    client.logout()
    assert client.get("/metagame").status_code == 302
