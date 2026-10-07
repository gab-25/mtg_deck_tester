"""Tests for the effect DSL and its validator."""

import pytest

from playtest.engine.cardrules import characteristics
from playtest.engine.dsl import OPS, SELECTORS, DslError, grounded_numbers, validate_program

ORACLE = (
    "Deal 3 damage to any target. Draw two cards. Add {R}{R}{R}. "
    "Create a 1/1 white Soldier creature token. Target creature gets +2/+2 until end of turn. "
    "Target creature with power 2 or less."
)

# One valid operation per op kind: if the vocabulary grows, this table must too.
SAMPLE_OPS = {
    "DEAL_DAMAGE": ({"op": "DEAL_DAMAGE", "target": "t", "amount": 3}, "ANY_TARGET"),
    "GAIN_LIFE": ({"op": "GAIN_LIFE", "target": "t", "amount": 3}, "YOU"),
    "LOSE_LIFE": ({"op": "LOSE_LIFE", "target": "t", "amount": 2}, "EACH_OPPONENT"),
    "DRAW_CARDS": ({"op": "DRAW_CARDS", "target": "t", "amount": 2}, "YOU"),
    "DISCARD_CARDS": ({"op": "DISCARD_CARDS", "target": "t", "amount": 1}, "TARGET_OPPONENT"),
    "DESTROY": ({"op": "DESTROY", "target": "t"}, "TARGET_CREATURE"),
    "EXILE": ({"op": "EXILE", "target": "t"}, "TARGET_NONLAND_PERMANENT"),
    "RETURN_TO_HAND": ({"op": "RETURN_TO_HAND", "target": "t"}, "TARGET_PERMANENT"),
    "TAP": ({"op": "TAP", "target": "t"}, "TARGET_CREATURE"),
    "UNTAP": ({"op": "UNTAP", "target": "t"}, "TARGET_LAND"),
    "ADD_MANA": ({"op": "ADD_MANA", "amount": 3, "color": "R"}, None),
    "CREATE_TOKEN": (
        {
            "op": "CREATE_TOKEN",
            "target": "t",
            "amount": 1,
            "power": 1,
            "toughness": 1,
            "token_name": "Soldier",
        },
        "YOU",
    ),
    "MODIFY_PT": (
        {"op": "MODIFY_PT", "target": "t", "power": 2, "toughness": 2, "duration": "END_OF_TURN"},
        "TARGET_CREATURE",
    ),
    "GRANT_KEYWORD": (
        {"op": "GRANT_KEYWORD", "target": "t", "keyword": "flying", "duration": "END_OF_TURN"},
        "EACH_CREATURE_YOU_CONTROL",
    ),
    "PUT_COUNTERS": (
        {"op": "PUT_COUNTERS", "target": "t", "amount": 2, "counter": "+1/+1"},
        "SELF",
    ),
    "SACRIFICE": (
        {"op": "SACRIFICE", "target": "t", "amount": 1, "card_type": "CREATURE"},
        "EACH_OPPONENT",
    ),
    "MILL": ({"op": "MILL", "target": "t", "amount": 3}, "TARGET_PLAYER"),
    "COUNTER_SPELL": ({"op": "COUNTER_SPELL", "target": "t"}, "TARGET_SPELL"),
}


@pytest.fixture
def facts():
    return characteristics(
        {"oracle_id": "o", "name": "Test", "type_line": "Instant", "oracle_text": ORACLE}
    )


def ability(ops, targets=(), trigger="SPELL", cost=None, mode_group=None):
    return {
        "trigger": trigger,
        "cost": cost,
        "mode_group": mode_group,
        "targets": list(targets),
        "ops": ops,
    }


def target(selector, id="t", restriction=None):
    return {"id": id, "selector": selector, "restriction": restriction}


def program(*abilities):
    return {"abilities": list(abilities)}


def test_the_vocabulary_is_the_closed_list_of_eighteen():
    assert set(OPS) == set(SAMPLE_OPS)
    assert len(OPS) == 18
    assert all(spec.description for spec in OPS.values())
    assert all(description for _, description in SELECTORS.values())


@pytest.mark.parametrize("kind", sorted(SAMPLE_OPS))
def test_the_validator_accepts_every_op_of_the_vocabulary(kind, facts):
    op, selector = SAMPLE_OPS[kind]
    targets = [target(selector)] if selector else []
    result = validate_program(program(ability([op], targets)), facts)
    assert result.abilities[0].ops[0].kind == kind


def test_a_valid_program_becomes_typed_objects(facts):
    result = validate_program(
        program(
            ability(
                [{"op": "DEAL_DAMAGE", "target": "t", "amount": 3}],
                [target("TARGET_CREATURE", restriction={"kind": "POWER_LE", "value": 2})],
            )
        ),
        facts,
    )
    [only] = result.abilities
    assert only.ops[0].amount == 3
    assert only.targets[0].restriction.kind == "POWER_LE"


def test_an_empty_program_is_valid(facts):
    assert validate_program(program(), facts).abilities == ()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"abilities": [], "extra": 1}, "unknown key 'extra'"),
        (program(ability([{"op": "FIREBALL", "target": "t"}], [target("ANY_TARGET")])), "FIREBALL"),
        (
            program(
                ability(
                    [{"op": "DESTROY", "target": "t", "amount": 3}], [target("TARGET_CREATURE")]
                )
            ),
            "unknown key 'amount'",
        ),
        (
            program(ability([{"op": "DEAL_DAMAGE", "target": "t"}], [target("ANY_TARGET")])),
            "missing key 'amount'",
        ),
        (
            program(ability([{"op": "DESTROY", "target": "t2"}], [target("TARGET_CREATURE")])),
            "not a declared target",
        ),
        (
            program(
                ability(
                    [{"op": "DRAW_CARDS", "target": "t", "amount": 2}],
                    [target("YOU"), target("ANY_TARGET", id="u")],
                )
            ),
            "declared but never used",
        ),
        (program(ability([{"op": "DESTROY", "target": "t"}], [target("YOU")])), "can't affect YOU"),
        (
            program(
                ability([{"op": "DEAL_DAMAGE", "target": "t", "amount": 7}], [target("ANY_TARGET")])
            ),
            "7 does not appear",
        ),
        (
            program(
                ability(
                    [{"op": "DEAL_DAMAGE", "target": "t", "amount": 99}], [target("ANY_TARGET")]
                )
            ),
            "between 1 and 20",
        ),
        (
            program(
                ability(
                    [{"op": "DESTROY", "target": "t"}],
                    [target("TARGET_CREATURE")],
                    trigger="ACTIVATED",
                )
            ),
            "needs a cost",
        ),
        (
            program(
                ability(
                    [{"op": "DESTROY", "target": "t"}],
                    [target("TARGET_CREATURE")],
                    trigger="ACTIVATED",
                    cost={"mana": "", "tap": False, "sacrifice_self": False},
                )
            ),
            "no cost",
        ),
        (
            program(
                ability(
                    [{"op": "DESTROY", "target": "t"}],
                    [target("TARGET_CREATURE")],
                    trigger="ACTIVATED",
                    cost={"mana": "{W/P}", "tap": False, "sacrifice_self": False},
                )
            ),
            "unsupported mana symbol",
        ),
        (
            program(
                ability(
                    [
                        {"op": "ADD_MANA", "amount": 3, "color": "R"},
                        {"op": "DRAW_CARDS", "target": "t", "amount": 2},
                    ],
                    [target("YOU")],
                    trigger="ACTIVATED",
                    cost={"mana": "", "tap": True, "sacrifice_self": False},
                )
            ),
            "mana ability can only add mana",
        ),
        (
            program(
                ability(
                    [{"op": "DRAW_CARDS", "target": "t", "amount": 2}],
                    [target("YOU", restriction={"kind": "POWER_LE", "value": 2})],
                )
            ),
            "can't be restricted",
        ),
        (
            program(
                ability(
                    [{"op": "DESTROY", "target": "t"}],
                    [target("TARGET_CREATURE")],
                    trigger="SPELL",
                    cost={"mana": "{1}", "tap": False, "sacrifice_self": False},
                )
            ),
            "only an activated ability",
        ),
        (program(ability([], [])), "1 to 6 operations"),
    ],
)
def test_the_validator_rejects_never_coerces(payload, message, facts):
    with pytest.raises(DslError, match=message):
        validate_program(payload, facts)


def test_numbers_are_grounded_in_digits_words_and_mana_symbols():
    numbers = grounded_numbers("Draw two cards. Deal 3 damage. Add {G}{G}{G}{G}. Draw a card.")
    assert {1, 2, 3, 4} <= numbers
    assert 5 not in numbers
