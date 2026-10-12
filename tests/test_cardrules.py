"""Tests for the deterministic pre-pass, the mana-cost parser and the clause splitter."""

import pytest

from playtest.engine.cardrules import AltCost, ResidualClass, characteristics
from playtest.engine.clauses import SplitError, split_abilities
from playtest.engine.dsl import Trigger
from playtest.engine.manacost import ManaCost, ManaCostError, parse_mana_cost

from .cards import basic_land, raw_card


@pytest.mark.parametrize(
    "text, cost",
    [
        ("", ManaCost()),
        ("{3}{G}{G}", ManaCost(3, ("G", "G"))),
        ("{X}{R}", ManaCost(0, ("R",), 1)),
        ("{G/W}{U}", ManaCost(0, ("U", "WG"))),
        ("{2/W}", ManaCost(0, ("W",))),
        ("{B/P}", ManaCost(0, ("B",))),
        ("{C}{C}", ManaCost(0, ("C", "C"))),
    ],
)
def test_parse_mana_cost(text, cost):
    assert parse_mana_cost(text) == cost


@pytest.mark.parametrize("text", ["{Q}", "2G", "{G}{", "{H/W}"])
def test_parse_mana_cost_rejects_what_it_cannot_read(text):
    with pytest.raises(ManaCostError):
        parse_mana_cost(text)


def test_basic_land_needs_no_judge_and_taps_for_its_color():
    facts = characteristics(basic_land("Island", "U"))
    assert facts.residual_class == ResidualClass.EMPTY
    assert facts.is_land and facts.produced_mana == ("U",) and facts.mana_amount == 1


def test_mana_production_comes_from_produced_mana():
    sol_ring = characteristics(
        raw_card("Sol Ring", "Artifact", "{1}", "{T}: Add {C}{C}.", produced_mana=["C"])
    )
    assert sol_ring.produced_mana == ("C",) and sol_ring.mana_amount == 2
    assert sol_ring.residual_class == ResidualClass.EMPTY
    # The text says nothing the mana field doesn't: the field wins.
    tower = characteristics(
        raw_card(
            "Command Tower",
            "Land",
            "",
            "{T}: Add one mana of any color in your commander's color identity.",
            produced_mana=["W", "U", "B", "R", "G"],
        )
    )
    assert tower.produced_mana == ("W", "U", "B", "R", "G") and tower.mana_amount == 1


def test_a_creature_with_a_paid_mana_ability_is_no_free_source():
    facts = characteristics(
        raw_card("Ritualist", oracle_text="{1}, {T}: Add {B}{B}.", produced_mana=["B"])
    )
    assert not facts.is_mana_source
    assert facts.residual_class == ResidualClass.NEEDS_JUDGE


def test_enters_tapped_and_keywords_are_read_without_the_judge():
    gate = characteristics(
        raw_card(
            "Selesnya Guildgate",
            "Land — Gate",
            "",
            "This land enters tapped.\n{T}: Add {G} or {W}.",
            produced_mana=["G", "W"],
        )
    )
    assert gate.enters_tapped and gate.residual_class == ResidualClass.EMPTY
    angel = characteristics(
        raw_card(
            "Serra Angel",
            "Creature — Angel",
            "{3}{W}{W}",
            "Flying, vigilance",
            "4",
            "4",
            keywords=["Flying", "Vigilance"],
        )
    )
    assert angel.keywords == ("flying", "vigilance")
    assert angel.residual_class == ResidualClass.EMPTY
    assert (angel.power, angel.toughness, angel.mana_value, angel.colors) == (4, 4, 5, ("W",))


def test_ward_keeps_its_cost():
    facts = characteristics(raw_card("Warded", oracle_text="Ward {2}", keywords=["Ward"]))
    assert facts.keywords == ("ward",) and facts.ward == 2


def test_an_unsupported_keyword_is_blocked():
    facts = characteristics(raw_card("Hierarch", oracle_text="Exalted", keywords=["Exalted"]))
    assert facts.residual_class == ResidualClass.BLOCKED
    assert "exalted" in facts.block_reason


def test_a_keyword_standing_for_a_trigger_becomes_its_rules_text():
    facts = characteristics(raw_card("Monk", oracle_text="Prowess", keywords=["Prowess"]))
    assert facts.residual == "whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn."


@pytest.mark.parametrize(
    "oracle_text, reason",
    [
        ("Search target player's library for a card.", "library search"),
        ("Cascade", "cascade"),
        ("Creatures you control get +1/+1 for each Elf you control.", "for each"),
        ("Target creature can't block this turn.", "can't"),
        ("Exile target creature until this creature leaves the battlefield.", "exile until"),
    ],
)
def test_blocklisted_text_is_refused_without_a_model(oracle_text, reason):
    facts = characteristics(raw_card("X", "Sorcery", "{2}", oracle_text))
    assert facts.residual_class == ResidualClass.BLOCKED
    assert reason in facts.block_reason


def test_planeswalkers_are_blocked_by_type_not_by_word():
    walker = characteristics(raw_card("Jace", "Legendary Planeswalker — Jace", "{2}{U}{U}", "+1: Draw a card."))
    assert walker.residual_class == ResidualClass.BLOCKED
    murder = characteristics(raw_card("Hero's Downfall", "Instant", "{1}{B}{B}", "Destroy target creature or planeswalker."))
    assert murder.residual_class == ResidualClass.NEEDS_JUDGE


def test_regeneration_clauses_are_dropped():
    facts = characteristics(raw_card("Wrath", "Sorcery", "{2}{W}{W}", "Destroy all creatures. They can't be regenerated."))
    assert facts.residual == "destroy all creatures."


def test_the_residual_names_the_card_by_its_type():
    facts = characteristics(
        raw_card("Krenko, Mob Boss", "Legendary Creature — Goblin", oracle_text="{T}: Krenko deals 1 damage to any target.")
    )
    assert facts.residual == "{t}: this creature deals 1 damage to any target."
    assert facts.legendary


def test_double_faced_cards_use_the_front_face_and_are_partial():
    card = raw_card("Front // Back", "Creature — Human // Creature — Werewolf", "")
    card["card_faces"] = [
        {"name": "Front", "type_line": "Creature — Human", "mana_cost": "{1}{G}", "oracle_text": "", "power": "2", "toughness": "3"},
        {"name": "Back", "type_line": "Creature — Werewolf", "mana_cost": "", "oracle_text": "Trample", "power": "4", "toughness": "4"},
    ]
    facts = characteristics(card)
    assert facts.name == "Front" and facts.partial
    assert (facts.power, facts.toughness, facts.mana_value) == (2, 3, 2)


# -- the clause splitter ---------------------------------------------------------


def _split(oracle_text, type_line="Instant", cost="{1}"):
    facts = characteristics(raw_card("Card", type_line, cost, oracle_text))
    return split_abilities(facts.residual, facts)


def test_split_reads_triggers_and_keeps_clause_order():
    [ability] = _split("When this creature enters, you draw a card and you lose 1 life.", "Creature — Cat")
    assert ability.trigger == Trigger.ETB
    assert [c.text for c in ability.clauses] == ["you draw a card", "you lose 1 life"]
    assert ability.clauses[1].amounts == (1,)


def test_split_shares_the_subject_and_the_duration():
    [ability] = _split("Target creature gets +2/+2 and gains trample until end of turn.")
    assert [c.text for c in ability.clauses] == [
        "target creature gets +2/+2 until end of turn",
        "target creature gains trample until end of turn",
    ]
    assert ability.clauses[1].shares_target_with == 0
    assert ability.clauses[0].target_phrases == ("target creature",)


def test_split_parses_activated_costs():
    [ability] = _split("{2}, {T}, Sacrifice this artifact: Draw a card.", "Artifact")
    assert ability.trigger == Trigger.ACTIVATED
    assert (ability.cost.mana, ability.cost.tap, ability.cost.sacrifice_self) == ("{2}", True, True)


def test_split_turns_modes_into_abilities_sharing_a_group():
    abilities = _split("Choose one —\n• Destroy target artifact.\n• Destroy target enchantment.")
    assert [a.mode_group for a in abilities] == [0, 0]
    assert [a.clauses[0].text for a in abilities] == ["destroy target artifact", "destroy target enchantment"]


@pytest.mark.parametrize(
    "oracle_text, reason",
    [
        ("You may draw a card.", "may"),
        ("Exile target creature. Return it to the battlefield.", "back-reference"),
        ("Whenever a creature dies, you gain 1 life.", "trigger"),
        ("Return target creature card from your graveyard to your hand.", "zone"),
        ("Choose two —\n• Draw a card.\n• Gain 3 life.", "choose one"),
        ("Exile a card from your hand: Draw a card.", "cost"),
    ],
)
def test_split_refuses_what_it_cannot_read(oracle_text, reason):
    with pytest.raises(SplitError, match=reason):
        _split(oracle_text)


# -- the mana base is never a guess ------------------------------------------------------


def test_conditional_mana_is_not_read_as_a_free_source():
    cradle = characteristics(
        raw_card("Gaea's Cradle", "Legendary Land", "", "{T}: Add {G} for each creature you control.", produced_mana=["G"])
    )
    assert not cradle.is_mana_source
    assert cradle.residual_class == ResidualClass.BLOCKED


def test_a_costed_ability_does_not_lend_its_colors_to_the_free_one():
    nykthos = characteristics(
        raw_card(
            "Nykthos",
            "Legendary Land",
            "",
            "{T}: Add {C}.\n{2}, {T}: Choose a color. Add mana of that color.",
            produced_mana=["W", "U", "B", "R", "G", "C"],
        )
    )
    assert nykthos.produced_mana == ("C",)


def test_a_painland_taps_for_colorless_free_and_colors_for_damage():
    from playtest.engine.cardrules import ManaAbility

    pain = characteristics(
        raw_card(
            "Pain",
            "Land",
            "",
            "{T}: Add {C}.\n{T}: Add {R} or {G}. This land deals 1 damage to you.",
            produced_mana=["R", "G", "C"],
        )
    )
    assert pain.mana_abilities == (ManaAbility("C"), ManaAbility("RG", life=1))
    assert pain.residual_class == ResidualClass.EMPTY


def test_costed_mana_sources_are_read_without_the_judge():
    from playtest.engine.cardrules import ManaAbility

    tomb = characteristics(raw_card("Tomb", "Land", "", "{T}: Add {C}{C}. This land deals 2 damage to you.", produced_mana=["C"]))
    assert tomb.mana_abilities == (ManaAbility("C", amount=2, life=2),)
    petal = characteristics(raw_card("Petal", "Artifact", "{0}", "{T}, Sacrifice this artifact: Add one mana of any color.", produced_mana=list("WUBRG")))
    assert petal.mana_abilities == (ManaAbility("WUBRG", sacrifice=True),)
    town = characteristics(raw_card("Town", "Land", "", "{T}: Add {C}.\n{T}, Pay 1 life: Add one mana of any color.", produced_mana=list("WUBRGC")))
    assert town.mana_abilities[1] == ManaAbility("WUBRG", life=1)
    verge = characteristics(
        raw_card("Verge", "Land", "", "{T}: Add {B}.\n{T}: Add {R}. Activate only if you control a Swamp or a Mountain.", produced_mana=["B", "R"])
    )
    assert verge.mana_abilities == (ManaAbility("B"), ManaAbility("R", needs=("swamp", "mountain")))
    assert all(f.residual_class == ResidualClass.EMPTY for f in (tomb, petal, town, verge))


def test_typed_lands_read_their_reminder_text():
    pool = characteristics(
        raw_card("Breeding Pool", "Land — Forest Island", "", "({T}: Add {G} or {U}.)", produced_mana=["G", "U"])
    )
    assert pool.produced_mana == ("U", "G") and pool.mana_amount == 1


def test_costed_mana_abilities_are_refused():
    with pytest.raises(SplitError, match="mana abilities with a cost"):
        _split("{1}, {T}: Add {U}{B}.", "Artifact")


def test_modes_on_a_triggered_ability_are_refused():
    with pytest.raises(SplitError, match="modes"):
        _split("When this creature enters, choose one —\n• You gain 3 life.\n• Draw a card.", "Creature — Elf")


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "You may pay 1 life and exile a blue card from your hand rather than pay this spell's mana cost.",
            AltCost(life=1, exile_color="U", exile_count=1),
        ),
        (
            "If it's not your turn, you may exile a blue card from your hand rather than pay this spell's mana cost.",
            AltCost(exile_color="U", exile_count=1, condition="NOT_YOUR_TURN"),
        ),
        ("You may return an Island you control to its owner's hand rather than pay this spell's mana cost.", AltCost(return_land="island")),
        ("If you control a commander, you may cast this spell without paying its mana cost.", AltCost(condition="CONTROL_COMMANDER")),
    ],
)
def test_alternative_costs_are_read_in_code(text, expected):
    facts = characteristics(raw_card("Spell", "Instant", "{3}{U}", text + "\nDraw a card."))
    assert facts.alt_costs == (expected,)
    assert facts.residual == "draw a card."


@pytest.mark.parametrize(
    "text",
    [
        "You may discard two cards rather than pay this spell's mana cost.",
        "You may cast this spell without paying its mana cost.",
    ],
)
def test_an_alternative_cost_it_cannot_check_stays_in_the_text(text):
    facts = characteristics(raw_card("Spell", "Instant", "{3}{U}", text))
    assert facts.alt_costs == () and facts.residual


def test_casting_keywords_and_additional_costs():
    facts = characteristics(
        raw_card(
            "Spell",
            "Sorcery",
            "{1}{R}",
            "As an additional cost to cast this spell, sacrifice a creature.\nThis spell can't be countered.\nDraw a card.\nFlashback {2}{R}\nMountaincycling {1}",
            keywords=["Flashback", "Mountaincycling"],
        )
    )
    assert (facts.extra_sacrifice, facts.uncounterable, facts.flashback) == ("creature", True, "{2}{R}")
    assert (facts.cycling, facts.landcycling) == ("{1}", "TYPES:mountain")
    assert facts.residual == "draw a card."
