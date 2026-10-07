"""Tests for ``manage.py card_coverage`` (no network: cards come from the cache)."""

import io

import pytest
from django.core.management import CommandError, call_command

from decks.cache import DbCardCache
from tests.factories import make_deck


def _cache_cards():
    cache = DbCardCache()
    cache.set_card(
        "card_en_forest",
        {
            "oracle_id": "o-forest",
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "produced_mana": ["G"],
        },
    )
    cache.set_card(
        "card_en_lightning_bolt",
        {
            "oracle_id": "o-bolt",
            "name": "Lightning Bolt",
            "type_line": "Instant",
            "mana_cost": "{R}",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        },
    )
    cache.set_card(
        "card_en_bear_lord",
        {
            "oracle_id": "o-lord",
            "name": "Bear Lord",
            "type_line": "Legendary Creature — Bear",
            "mana_cost": "{3}{G}",
            "power": "5",
            "toughness": "5",
            "oracle_text": "",
        },
    )


def _run(*args):
    out = io.StringIO()
    call_command("card_coverage", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_prints_the_report_for_a_decklist_file_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _cache_cards()
    decklist = tmp_path / "deck.txt"
    decklist.write_text("Commander\n1 Bear Lord\nDeck\n10 Forest\n2 Lightning Bolt\n")

    output = _run(str(decklist))

    assert "Coverage: 84% of 13 cards" in output
    assert "WARNING: 2 dead spells" in output
    assert "not_asked: 2" in output
    assert "2 Lightning Bolt [not_asked] no OpenRouter API key configured" in output


@pytest.mark.django_db
def test_prints_the_report_for_an_imported_deck(django_user_model, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _cache_cards()
    owner = django_user_model.objects.create_user("owner", password="pw")
    cards = [
        {"quantity": 1, "is_commander": True, "data": {"name": "Bear Lord"}},
        {"quantity": 9, "is_commander": False, "data": {"name": "Forest"}},
    ]
    deck = make_deck(owner, cards=cards)

    assert "Coverage: 100% of 10 cards" in _run("--deck", str(deck.pk))


@pytest.mark.django_db
@pytest.mark.parametrize("args", [(), ("deck.txt", "--deck", "x"), ("--deck", "not-a-uuid")])
def test_bad_arguments_are_command_errors(args):
    with pytest.raises(CommandError):
        _run(*args)
