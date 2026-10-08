"""Tests for mana payment: max-flow feasibility and deterministic auto-tapping."""

from dataclasses import replace

import pytest

from playtest.engine.cards import CardSpec, Kind
from playtest.engine.mana import ManaSource, can_pay, mana_sources, payment
from playtest.engine.manacost import parse_mana_cost
from playtest.engine.state import Card, Permanent, PlayerState

from .factories import creature

_next_id = iter(range(1, 10_000))


def land(name, colors):
    return CardSpec(name, Kind.LAND, produced_mana=tuple(colors), types=("Land",))


FOREST = land("Forest", "G")
ISLAND = land("Island", "U")
PLAINS = land("Plains", "W")
TROPICAL = land("Tropical Island", "GU")
CITY = land("City of Brass", "WUBRG")
WASTES = land("Wastes", "C")
SOL_RING = CardSpec("Sol Ring", Kind.PERMANENT, 1, produced_mana=("C",), mana_amount=2, types=("Artifact",))
ELF = replace(creature("Llanowar Elves", 1, mana_value=1), produced_mana=("G",))


def player(*specs, sick=False) -> PlayerState:
    owner = PlayerState(seat=0, deck_name="P", life=20)
    for spec in specs:
        card = Card(next(_next_id), spec, 0)
        owner.battlefield.append(Permanent(next(_next_id), card, 0, sick=sick))
    return owner


def paid(owner, cost, extra=0):
    sources = payment(mana_sources(owner), parse_mana_cost(cost), extra)
    return None if sources is None else sorted(s.name for s in sources)


def test_a_green_cost_does_not_tap_the_only_blue_source():
    owner = player(ISLAND, FOREST, FOREST)
    assert paid(owner, "{1}{G}") == ["Forest", "Forest"]


def test_a_dual_land_is_kept_when_a_basic_can_pay():
    owner = player(TROPICAL, FOREST)
    assert paid(owner, "{G}") == ["Forest"]


def test_an_any_color_source_is_kept_when_basics_can_pay():
    owner = player(CITY, FOREST, ISLAND)
    assert paid(owner, "{1}{G}") == ["Forest", "Island"]


def test_the_same_state_always_produces_the_same_payment():
    owner = player(PLAINS, ISLAND, FOREST, TROPICAL, CITY, SOL_RING)
    first = payment(mana_sources(owner), parse_mana_cost("{2}{U}"))
    for _ in range(5):
        assert payment(mana_sources(owner), parse_mana_cost("{2}{U}")) == first


@pytest.mark.parametrize(
    "specs, cost, expected",
    [
        ((FOREST, FOREST), "{G}{G}", True),
        ((FOREST, ISLAND), "{G}{G}", False),
        ((SOL_RING,), "{2}", True),
        ((SOL_RING,), "{G}", False),
        ((SOL_RING, FOREST), "{2}{G}", True),
        ((CITY, CITY), "{W}{B}", True),
        ((ISLAND,), "{G/U}", True),
        ((PLAINS,), "{2/W}", True),
        ((FOREST, FOREST), "{2/W}", True),
        ((FOREST,), "{2/W}", False),
        ((WASTES, FOREST), "{C}{G}", True),
        ((FOREST, FOREST), "{C}", False),
        ((), "", True),
    ],
)
def test_feasibility(specs, cost, expected):
    assert can_pay(mana_sources(player(*specs)), parse_mana_cost(cost)) is expected


def test_greedy_would_fail_here_but_the_flow_does_not():
    # Tapping the Tropical Island for G first leaves no blue: the flow routes around it.
    owner = player(TROPICAL, FOREST)
    assert paid(owner, "{G}{U}") == ["Forest", "Tropical Island"]


def test_sol_ring_pays_two_and_lands_go_before_rocks():
    assert paid(player(SOL_RING), "{2}") == ["Sol Ring"]
    assert paid(player(SOL_RING, WASTES), "{1}") == ["Wastes"]


def test_extra_generic_is_added_for_tax_and_x():
    owner = player(FOREST, FOREST, FOREST)
    assert can_pay(mana_sources(owner), parse_mana_cost("{G}"), 2)
    assert not can_pay(mana_sources(owner), parse_mana_cost("{G}"), 3)


def test_summoning_sick_creatures_and_tapped_sources_make_no_mana():
    sick = player(ELF, sick=True)
    assert mana_sources(sick) == []
    ready = player(ELF)
    assert [s.name for s in mana_sources(ready)] == ["Llanowar Elves"]
    ready.battlefield[0].tapped = True
    assert mana_sources(ready) == []


def test_creature_sources_are_spent_last():
    owner = player(ELF, FOREST)
    assert paid(owner, "{G}") == ["Forest"]
    assert [s.rank for s in mana_sources(owner)] == [0, 2]


def test_sources_are_ordered_fewest_colors_first():
    sources = mana_sources(player(CITY, TROPICAL, FOREST))
    assert [s.name for s in sources] == ["Forest", "Tropical Island", "City of Brass"]
    assert all(isinstance(s, ManaSource) for s in sources)


def test_the_post_pass_swaps_a_spent_source_to_keep_more_colors():
    # Offered in a bad order, the flow first spends the City of Brass on {G}; the
    # swap gives the Forest's job back to the Forest and keeps every color open.
    sources = [
        ManaSource(1, "City of Brass", tuple("WUBRG"), 1, 0),
        ManaSource(2, "Forest", ("G",), 1, 0),
        ManaSource(3, "Island", ("U",), 1, 0),
    ]
    assert [s.name for s in payment(sources, parse_mana_cost("{G}"))] == ["Forest"]
