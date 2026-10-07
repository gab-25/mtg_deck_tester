"""Tests for the clause splitter (residual oracle text -> abilities and clauses)."""

import pytest

from playtest.engine.cardrules import characteristics
from playtest.engine.clauses import Unreadable, split_abilities


def split(oracle_text, type_line="Instant", name="Test Card"):
    facts = characteristics(
        {"oracle_id": "o", "name": name, "type_line": type_line, "oracle_text": oracle_text}
    )
    return split_abilities(facts.residual, facts)


def test_a_spell_is_one_spell_ability_with_one_clause():
    [ability] = split("Test Card deals 3 damage to any target.")
    assert ability.trigger == "SPELL"
    assert ability.cost is None
    [clause] = ability.clauses
    assert clause.text == "Test Card deals 3 damage to any target"
    assert clause.amounts == (3,)
    assert clause.has_target


def test_sentences_and_verb_conjunctions_split_into_clauses_in_order():
    [ability] = split("Destroy target creature. You gain 2 life and draw a card.")
    assert [c.text for c in ability.clauses] == [
        "Destroy target creature",
        "You gain 2 life",
        "You draw a card",
    ]
    assert ability.clauses[2].amounts == (1,)


def test_a_continuation_keeps_its_subject():
    [ability] = split("Target player draws two cards and loses 2 life.")
    assert [c.text for c in ability.clauses] == [
        "Target player draws two cards",
        "Target player loses 2 life",
    ]
    assert [c.amounts for c in ability.clauses] == [(2,), (2,)]
    assert [(c.has_target, c.inherits_target) for c in ability.clauses] == [
        (True, False),
        (False, True),
    ]


def test_a_permanent_gets_static_and_triggered_abilities():
    abilities = split(
        "Creatures you control get +1/+1.\nWhen Test Card enters, draw a card.\nAt the beginning of your upkeep, you gain 1 life.",
        type_line="Creature — Elf",
    )
    assert [a.trigger for a in abilities] == ["STATIC", "ETB", "UPKEEP_TRIGGER"]
    assert abilities[0].clauses[0].pt_mods == ((1, 1),)
    assert abilities[0].clauses[0].duration == "PERMANENT"


def test_this_creature_counts_as_a_self_reference():
    [ability] = split(
        "When this creature dies, each opponent loses 2 life.", type_line="Creature — Zombie"
    )
    assert ability.trigger == "DIES"


def test_a_name_with_commas_and_apostrophes_is_a_self_reference():
    [ability] = split(
        "When Atraxa, Praetors' Voice enters, draw a card.",
        type_line="Legendary Creature — Angel",
        name="Atraxa, Praetors' Voice",
    )
    assert ability.trigger == "ETB"
    assert [c.text for c in ability.clauses] == ["draw a card"]


def test_an_activated_ability_has_its_cost_parsed_in_code():
    [ability] = split("{2}{R}, {T}: Test Card deals 2 damage to any target.", type_line="Artifact")
    assert ability.trigger == "ACTIVATED"
    assert ability.cost == {"mana": "{2}{R}", "tap": True, "sacrifice_self": False}


def test_a_sacrifice_self_cost():
    [ability] = split("Sacrifice Test Card: Draw a card.", type_line="Artifact")
    assert ability.cost == {"mana": "", "tap": False, "sacrifice_self": True}


def test_choose_one_becomes_modes_sharing_a_group():
    abilities = split("Choose one —\n• Destroy target artifact.\n• Destroy target enchantment.")
    assert [a.mode_group for a in abilities] == [1, 1]
    assert [a.clauses[0].text for a in abilities] == [
        "Destroy target artifact",
        "Destroy target enchantment",
    ]


def test_candidates_are_extracted_for_every_field():
    [ability] = split("Create two 1/1 white Soldier creature tokens.")
    [clause] = ability.clauses
    assert clause.amounts == (2,)
    assert clause.token_pts == ((1, 1),)
    assert clause.token_names == ("white Soldier",)

    [ability] = split("Target creature gets +3/+3 and gains trample until end of turn.")
    assert [c.text for c in ability.clauses] == [
        "Target creature gets +3/+3",
        "gains trample until end of turn",
    ]

    [ability] = split("Put two +1/+1 counters on target creature.")
    assert ability.clauses[0].counters == ("+1/+1",)
    assert ability.clauses[0].amounts == (2,)
    assert ability.clauses[0].pt_mods == ()

    [ability] = split("Add {R}{R}{R}.")
    assert ability.clauses[0].amounts == (3,)
    assert ability.clauses[0].colors == ("R",)

    [ability] = split("Each opponent sacrifices a creature.")
    assert ability.clauses[0].card_types == ("CREATURE",)
    assert ability.clauses[0].amounts == (1,)


def test_a_power_restriction_is_read_in_code():
    [ability] = split("Destroy target creature with power 3 or less.")
    clause = ability.clauses[0]
    assert clause.restriction == {"kind": "POWER_LE", "value": 3}
    assert clause.amounts == ()


@pytest.mark.parametrize(
    "oracle_text, type_line, reason",
    [
        ("Whenever a creature enters, you gain 1 life.", "Enchantment", "unsupported trigger"),
        ("{T}, Pay 1 life: Draw a card.", "Artifact", "unsupported cost"),
        ("Destroy target creature. Its controller gains 3 life.", "Instant", "refers back"),
        ("You may draw a card.", "Sorcery", "optional effect"),
        ("Destroy target attacking creature.", "Instant", "unsupported qualifier"),
        ("Destroy target creature with flying.", "Instant", "unsupported qualifier"),
        (
            "Test Card deals 2 damage to target creature and 2 damage to you.",
            "Instant",
            "several players",
        ),
        ("Choose two —\n• Draw a card.\n• Gain 3 life.", "Sorcery", "other than 'choose one'"),
        # Review fixes: never guess a subject, a timing, a scope or a number.
        (
            "Whenever this creature attacks, defending player loses 2 life.",
            "Creature",
            "no recognised subject",
        ),
        (
            "Enchanted creature's controller loses 2 life.",
            "Enchantment — Aura",
            "no recognised subject",
        ),
        (
            "Enrage — Whenever this creature is dealt damage, create a 1/1 green Saproling creature token.",
            "Creature — Dinosaur",
            "ability word",
        ),
        ("If a creature died this turn, draw two cards.", "Sorcery", "conditional or delayed"),
        (
            "When this creature enters, if a creature died this turn, draw a card.",
            "Creature",
            "conditional or delayed",
        ),
        (
            "Draw a card at the beginning of the next turn's upkeep.",
            "Sorcery",
            "conditional or delayed",
        ),
        (
            'Create two 1/1 colorless Eldrazi Spawn creature tokens with "Sacrifice this creature: Add {C}."',
            "Sorcery",
            "quoted ability",
        ),
        ("Destroy all white creatures.", "Sorcery", "unsupported qualifier"),
        ("Other creatures you control get +1/+1.", "Creature — Elf", "unsupported qualifier"),
        ("Elf creatures you control get +1/+1.", "Creature — Elf", "unsupported qualifier"),
        ("Tap up to two target creatures.", "Instant", "unsupported qualifier"),
        ("Destroy two target artifacts.", "Sorcery", "unsupported qualifier"),
        ("Destroy another target creature.", "Sorcery", "unsupported qualifier"),
        (
            "Return target creature card from your graveyard to your hand.",
            "Sorcery",
            "card in a zone",
        ),
        ("Target creature gets +3/+3 until your next turn.", "Instant", "duration"),
        ("Create a 1/1 white Spirit creature token with flying.", "Sorcery", "token"),
        ("Test Card deals 2 damage to any target 3 times.", "Instant", "several numbers"),
    ],
)
def test_text_it_cannot_read_is_unreadable(oracle_text, type_line, reason):
    with pytest.raises(Unreadable, match=reason):
        split(oracle_text, type_line)


def test_a_single_counter_is_an_amount_of_one():
    [ability] = split("Put a +1/+1 counter on target creature.")
    assert ability.clauses[0].amounts == (1,)
    assert ability.clauses[0].counters == ("+1/+1",)


def test_a_legendary_card_refers_to_itself_by_its_short_name():
    [ability] = split(
        "When Atraxa enters, draw a card.",
        type_line="Legendary Creature — Angel",
        name="Atraxa, Praetors' Voice",
    )
    assert ability.trigger == "ETB"


def test_its_owners_hand_is_not_a_back_reference():
    [ability] = split("Return target creature to its owner's hand.")
    assert ability.clauses[0].has_target


def test_whole_board_phrases_still_read():
    [ability] = split("Destroy all creatures.", type_line="Sorcery")
    assert ability.clauses[0].has_target
