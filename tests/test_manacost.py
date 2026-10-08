"""Tests for the mana cost parser."""

import pytest

from playtest.engine.manacost import ManaCost, ManaCostError, parse_mana_cost


@pytest.mark.parametrize(
    "text, expected, mana_value",
    [
        ("", ManaCost(), 0),
        ("{R}", ManaCost(0, (("R", 1),)), 1),
        ("{2}{G}{G}", ManaCost(2, (("G", 2),)), 4),
        ("{X}{R}", ManaCost(0, (("R", 1),), 1), 1),
        ("{C}{C}", ManaCost(0, (("C", 2),)), 2),
        ("{1}{W/U}", ManaCost(1, (("W/U", 1),)), 2),
        ("{2/W}{2/W}", ManaCost(0, (("2/W", 2),)), 4),
        ("{10}", ManaCost(10), 10),
    ],
)
def test_parses_printed_costs(text, expected, mana_value):
    cost = parse_mana_cost(text)
    assert cost == expected
    assert cost.mana_value == mana_value


@pytest.mark.parametrize("text", ["{W/P}", "{S}", "{H}", "2G", "{G}{", "{G} and {U}"])
def test_rejects_costs_the_kernel_cannot_pay(text):
    with pytest.raises(ManaCostError):
        parse_mana_cost(text)
