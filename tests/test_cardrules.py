"""Tests for the deterministic pre-pass (Scryfall JSON -> card facts)."""

import pytest

from playtest.engine.cardrules import Residue, characteristics, printed_stat
from playtest.engine.manacost import ManaCost


def raw(name, type_line, oracle_text="", mana_cost="", cmc=0.0, **fields):
    """A raw Scryfall card, as cached in ``ScryfallCard.data``."""
    return {
        "oracle_id": f"oracle-{name}",
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "mana_cost": mana_cost,
        "cmc": cmc,
        "colors": fields.pop("colors", []),
        "produced_mana": fields.pop("produced_mana", []),
        **fields,
    }


def test_a_basic_land_produces_its_color_and_needs_no_judge():
    facts = characteristics(
        raw("Forest", "Basic Land — Forest", "({T}: Add {G}.)", produced_mana=["G"])
    )
    assert facts.residue is Residue.EMPTY
    assert facts.produced_mana == ("G",)
    assert facts.mana_amount == 1
    assert facts.supertypes == ("Basic",)
    assert facts.types == ("Land",)
    assert facts.subtypes == ("Forest",)


def test_mana_production_comes_from_produced_mana_and_the_printed_amount():
    facts = characteristics(
        raw("Sol Ring", "Artifact", "{T}: Add {C}{C}.", "{1}", 1.0, produced_mana=["C"])
    )
    assert facts.residue is Residue.EMPTY
    assert facts.produced_mana == ("C",)
    assert facts.mana_amount == 2


def test_a_tapped_dual_land_reads_entirely_off_its_text():
    facts = characteristics(
        raw(
            "Azorius Guildgate",
            "Land — Gate",
            "This land enters tapped.\n{T}: Add {W} or {U}.",
            produced_mana=["U", "W"],
        )
    )
    assert facts.residue is Residue.EMPTY
    assert facts.enters_tapped
    assert facts.produced_mana == ("W", "U")
    assert facts.mana_amount == 1


def test_a_vanilla_creature_is_fully_described_by_its_facts():
    facts = characteristics(
        raw(
            "Grizzly Bears",
            "Creature — Bear",
            "",
            "{1}{G}",
            2.0,
            colors=["G"],
            power="2",
            toughness="2",
        )
    )
    assert facts.residue is Residue.EMPTY
    assert (facts.power, facts.toughness) == (2, 2)
    assert facts.mana_cost == ManaCost(1, (("G", 1),))
    assert facts.mana_value == 2
    assert facts.colors == ("G",)


def test_keyword_lines_become_keywords():
    facts = characteristics(
        raw(
            "Serra Angel",
            "Creature — Angel",
            "Flying, vigilance",
            "{3}{W}{W}",
            5.0,
            power="4",
            toughness="4",
        )
    )
    assert facts.residue is Residue.EMPTY
    assert facts.keywords == ("flying", "vigilance")


def test_ward_keeps_its_cost():
    facts = characteristics(
        raw("Warded", "Creature — Spirit", "Ward {2}", "{1}{U}", 2.0, power="1", toughness="1")
    )
    assert facts.keywords == ("ward",)
    assert facts.ward_cost == ManaCost(2)
    assert facts.residue is Residue.EMPTY


def test_an_unsupported_keyword_stays_in_the_residual_text():
    facts = characteristics(
        raw("Monk", "Creature — Human Monk", "Prowess", "{U}", 1.0, power="1", toughness="2")
    )
    assert facts.residual == "Prowess"
    assert facts.residue is Residue.NEEDS_JUDGE


def test_rules_text_goes_to_the_judge_without_reminder_text():
    facts = characteristics(
        raw(
            "Lightning Bolt",
            "Instant",
            "Lightning Bolt deals 3 damage to any target. (It's a burn spell.)",
            "{R}",
            1.0,
        )
    )
    assert facts.residue is Residue.NEEDS_JUDGE
    assert facts.residual == "Lightning Bolt deals 3 damage to any target."


@pytest.mark.parametrize(
    "type_line, oracle_text, mana_cost, label",
    [
        (
            "Land",
            "{T}, Sacrifice Evolving Wilds: Search your library for a basic land card.",
            "",
            "search your library",
        ),
        ("Sorcery", "Cascade\nDraw a card.", "{2}{U}", "cascade"),
        (
            "Instant",
            "Counter target spell. Its controller can't cast spells this turn.",
            "{U}",
            "can't",
        ),
        ("Legendary Planeswalker — Jace", "+1: Draw a card.", "{1}{U}{U}", "planeswalker"),
        ("Enchantment — Saga", "I — Draw a card.", "{1}{U}", "saga"),
        ("Instant", "Destroy target creature.", "{W/P}", "mana cost {W/P}"),
    ],
)
def test_blocked_cards_never_reach_the_judge(type_line, oracle_text, mana_cost, label):
    facts = characteristics(raw("Card", type_line, oracle_text, mana_cost))
    assert facts.residue is Residue.BLOCKED
    assert facts.blocked_by == label


def test_a_double_faced_card_is_read_from_its_front_face_and_marked_partial():
    card = {
        "oracle_id": "o-mdfc",
        "name": "Sink into Stupor // Soporific Springs",
        "type_line": "Instant // Land",
        "cmc": 3.0,
        "card_faces": [
            {
                "name": "Sink into Stupor",
                "type_line": "Instant",
                "mana_cost": "{1}{U}{U}",
                "oracle_text": "Return target creature to its owner's hand.",
                "colors": ["U"],
            },
            {
                "name": "Soporific Springs",
                "type_line": "Land",
                "mana_cost": "",
                "oracle_text": "{T}: Add {U}.",
            },
        ],
    }
    facts = characteristics(card)
    assert facts.partial
    assert facts.name == "Sink into Stupor"
    assert facts.types == ("Instant",)
    assert facts.colors == ("U",)
    assert facts.residual == "Return target creature to its owner's hand."


@pytest.mark.parametrize("value, expected", [("3", 3), ("1+*", 1), ("*", 0), (None, 0)])
def test_printed_stat(value, expected):
    assert printed_stat(value) == expected
