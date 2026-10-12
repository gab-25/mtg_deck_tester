"""Tests for the effect DSL: its grammar and its validator."""

import copy

import pytest

from playtest.engine import dsl
from playtest.engine.cardrules import characteristics

from .cards import raw_card

BOLT = characteristics(raw_card("Bolt", "Instant", "{R}", "Bolt deals 3 damage to any target."))


def _program(*abilities):
    return {"version": dsl.DSL_VERSION, "abilities": list(abilities)}


def _bolt(**changes):
    ability = {
        "trigger": "SPELL",
        "targets": {"t1": {"selector": "ANY_TARGET"}},
        "ops": [{"op": "DEAL_DAMAGE", "target": "t1", "amount": 3}],
    }
    ability.update(changes)
    return _program(ability)


def test_a_valid_program_round_trips():
    program = dsl.validate_program(_bolt(), BOLT)
    assert program.abilities[0].ops[0] == dsl.Op("DEAL_DAMAGE", target="t1", amount=3)
    assert dsl.validate_program(dsl.to_json(program), BOLT) == program


@pytest.mark.parametrize(
    "mutate, path",
    [
        (lambda p: p.update(extra=1), "program"),
        (lambda p: p["abilities"][0]["ops"][0].update(op="BURN"), "op"),
        (lambda p: p["abilities"][0]["ops"][0].update(keyword="flying"), "keyword"),
        (lambda p: p["abilities"][0]["ops"][0].pop("amount"), "amount"),
        (lambda p: p["abilities"][0]["ops"][0].update(target="t2"), "target"),
        (lambda p: p["abilities"][0]["targets"].update(t2={"selector": "YOU"}), "t2"),
        (lambda p: p["abilities"][0]["targets"]["t1"].update(selector="EVERYONE"), "selector"),
        (lambda p: p["abilities"][0].update(trigger="SOMETIMES"), "trigger"),
        (lambda p: p["abilities"][0]["ops"][0].update(amount=4), "amount"),
        (lambda p: p["abilities"][0]["ops"][0].update(amount=99), "amount"),
        (lambda p: p["abilities"][0]["ops"][0].update(amount=True), "amount"),
        (lambda p: p["abilities"][0]["targets"]["t1"].update(selector="TARGET_SPELL"), "target"),
    ],
)
def test_the_validator_rejects_never_coerces(mutate, path):
    payload = _bolt()
    mutate(payload)
    with pytest.raises(dsl.DslError) as exc:
        dsl.validate_program(payload, BOLT)
    assert path in exc.value.path


def test_an_amount_absent_from_the_text_is_rejected():
    with pytest.raises(dsl.DslError, match="does not appear"):
        dsl.validate_program(_bolt(ops=[{"op": "DEAL_DAMAGE", "target": "t1", "amount": 5}]), BOLT)


def _permanent(text):
    return characteristics(raw_card("Thing", "Artifact", "{2}", text))


def test_an_activated_ability_needs_a_cost():
    facts = _permanent("{T}: Draw a card.")
    payload = _program(
        {
            "trigger": "ACTIVATED",
            "cost": {"mana": "", "tap": False, "sacrifice_self": False},
            "targets": {"t1": {"selector": "YOU"}},
            "ops": [{"op": "DRAW_CARDS", "target": "t1", "amount": 1}],
        }
    )
    with pytest.raises(dsl.DslError, match="needs a cost"):
        dsl.validate_program(payload, facts)
    payload["abilities"][0]["cost"]["tap"] = True
    dsl.validate_program(payload, facts)


def test_a_mana_ability_only_adds_mana():
    facts = _permanent("{1}, {T}: Add {B}{B}. You draw a card.")
    payload = _program(
        {
            "trigger": "ACTIVATED",
            "cost": {"mana": "{1}", "tap": True},
            "targets": {"t1": {"selector": "YOU"}},
            "ops": [{"op": "ADD_MANA", "mana": "{B}{B}"}, {"op": "DRAW_CARDS", "target": "t1", "amount": 1}],
        }
    )
    with pytest.raises(dsl.DslError, match="only add mana"):
        dsl.validate_program(payload, facts)


def test_a_static_ability_cannot_deal_damage_or_target():
    facts = _permanent("Creatures you control get +1/+1.")
    anthem = {
        "trigger": "STATIC",
        "targets": {"t1": {"selector": "EACH_CREATURE_YOU_CONTROL"}},
        "ops": [{"op": "MODIFY_PT", "target": "t1", "power": 1, "toughness": 1, "duration": "STATIC"}],
    }
    dsl.validate_program(_program(anthem), facts)
    bad = copy.deepcopy(anthem)
    bad["targets"]["t1"]["selector"] = "TARGET_CREATURE"
    with pytest.raises(dsl.DslError, match="cannot target"):
        dsl.validate_program(_program(bad), facts)


def test_x_needs_x_in_the_mana_cost():
    fireball = characteristics(raw_card("Blast", "Sorcery", "{X}{R}", "Blast deals X damage to any target."))
    payload = _bolt(ops=[{"op": "DEAL_DAMAGE", "target": "t1", "amount": "X"}])
    dsl.validate_program(payload, fireball)
    with pytest.raises(dsl.DslError, match="X"):
        dsl.validate_program(payload, BOLT)


def test_every_op_and_selector_has_a_description_and_a_grammar():
    assert len(dsl.OPS) == 21
    for name, spec in dsl.OPS.items():
        assert spec.description and spec.grammar, name
        assert set(spec.required) | set(spec.optional) <= dsl._OP_FIELDS, name
    for name, spec in dsl.SELECTORS.items():
        assert spec.description and spec.phrase, name


@pytest.mark.parametrize(
    "op, clause, fields",
    [
        ("DEAL_DAMAGE", "this spell deals 3 damage to any target", {"selector": "ANY_TARGET", "amount": 3}),
        ("DRAW_CARDS", "draw two cards", {"selector": "YOU", "amount": 2}),
        ("DRAW_CARDS", "target player draws a card", {"selector": "TARGET_PLAYER", "amount": 1}),
        ("LOSE_LIFE", "each opponent loses 2 life", {"selector": "EACH_OPPONENT", "amount": 2}),
        ("DESTROY", "destroy target creature with power 3 or less", {"selector": "TARGET_CREATURE", "restriction": {"kind": "POWER_LE", "value": 3}}),
        ("DESTROY", "destroy target tapped creature", {"selector": "TARGET_CREATURE", "restriction": {"kind": "TAPPED"}}),
        ("RETURN_TO_HAND", "return target nonland permanent to its owner's hand", {"selector": "TARGET_NONLAND_PERMANENT"}),
        ("ADD_MANA", "add {g}{g}", {"mana": "{G}{G}"}),
        ("ADD_MANA", "add one mana of any color", {"any_color": 1}),
        ("MODIFY_PT", "creatures you control get +1/+1", {"selector": "EACH_CREATURE_YOU_CONTROL", "power": 1, "toughness": 1, "duration": "STATIC"}),
        ("GRANT_KEYWORD", "target creature gains flying until end of turn", {"selector": "TARGET_CREATURE", "keyword": "flying", "duration": "EOT"}),
        ("PUT_COUNTERS", "put two +1/+1 counters on target creature", {"selector": "TARGET_CREATURE", "amount": 2, "counter": "+1/+1"}),
        ("SACRIFICE", "each opponent sacrifices a creature", {"selector": "EACH_OPPONENT", "amount": 1, "what": "creature"}),
        ("CREATE_TOKEN", "create two 1/1 white soldier creature tokens with vigilance", {"amount": 2, "power": 1, "toughness": 1, "colors": ["W"], "subtype": "soldier", "keywords": ["vigilance"]}),
        ("COUNTER_SPELL", "counter target noncreature spell", {"selector": "TARGET_NONCREATURE_SPELL"}),
    ],
)
def test_parse_clause_reads_the_fields_off_the_text(op, clause, fields):
    parsed = dsl.parse_clause(op, clause)
    assert parsed is not None
    for key, value in fields.items():
        assert parsed[key] == value, key


@pytest.mark.parametrize(
    "op, clause",
    [
        ("DESTROY", "destroy target nonblack creature"),
        ("DEAL_DAMAGE", "this spell deals 2 damage to each creature and each player"),
        ("DRAW_CARDS", "you lose 2 life"),
    ],
)
def test_parse_clause_refuses_what_the_grammar_does_not_cover(op, clause):
    assert dsl.parse_clause(op, clause) is None


def test_x_is_refused_outside_spell_abilities():
    facts = characteristics(raw_card("Hydra", "Creature — Hydra", "{X}{G}", "When this creature enters, put x +1/+1 counters on this creature."))
    payload = _program(
        {
            "trigger": "ETB",
            "targets": {"t1": {"selector": "SELF"}},
            "ops": [{"op": "PUT_COUNTERS", "target": "t1", "amount": "X", "counter": "+1/+1"}],
        }
    )
    with pytest.raises(dsl.DslError, match="X amounts only in spell"):
        dsl.validate_program(payload, facts)
