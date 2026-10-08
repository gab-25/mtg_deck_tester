"""Tests for the heuristic agent and its helpers over the player view."""

import copy

from playtest.agents.heuristic import (
    HeuristicAgent,
    attack_value,
    block_outcome,
    creature_value,
    incoming_damage,
    lethal_on_board,
    permanents_by_id,
)
from playtest.engine.actions import Action, ActionKind, legal_actions
from playtest.engine.rules import apply
from playtest.engine.state import Declaring, Step
from playtest.engine.view import player_view

from .factories import FOREST, bear, creature
from .test_engine import COMMANDER, _attack, _card, _declare, _perm, _table

AGENT = HeuristicAgent()


def _choose(state, seat):
    return AGENT.choose(player_view(state, seat), legal_actions(state, seat)).action


def test_it_plays_a_land_first_then_the_commander_then_the_biggest_creature():
    _, state = _table()
    _card(state, 0, FOREST)
    _card(state, 0, bear(1))
    _card(state, 0, creature("Ogre", 3, mana_value=3))
    for _ in range(4):
        _perm(state, 0, FOREST)
    assert _choose(state, 0).kind == ActionKind.PLAY_LAND
    apply(state, 0, _choose(state, 0))
    assert _choose(state, 0).kind == ActionKind.CAST_COMMANDER
    state.player(0).command.clear()
    state.total_cards -= 1
    assert _choose(state, 0).card_name == "Ogre"


def test_it_casts_nothing_outside_its_own_main_phase():
    _, state = _table()
    _card(state, 1, creature("Ambusher", 2, keywords=["flash"]))
    _perm(state, 1, FOREST)
    _perm(state, 1, FOREST)
    state.step = Step.BEGIN_COMBAT
    assert len(legal_actions(state, 1)) == 2
    assert _choose(state, 1).kind == ActionKind.PASS


def test_it_attacks_only_where_no_blocker_eats_its_creature_for_free():
    _, state = _table(players=3, rules=COMMANDER)
    bear_perm = _perm(state, 0, bear(1))
    _perm(state, 1, creature("Wall", 0, 5))  # survives and kills nothing: fine
    _perm(state, 2, creature("Ogre", 3, 3))  # kills the bear and survives
    view = player_view(state, 0)
    perm = permanents_by_id(view)[bear_perm.id]
    assert attack_value(view, perm, 1) > 0
    assert attack_value(view, perm, 2) == 0
    _declare(state, Declaring.ATTACKERS, 0)
    choice = _choose(state, 0)
    assert (choice.kind, choice.target_seat) == (ActionKind.DECLARE_ATTACKER, 1)


def test_it_prefers_the_opponent_closest_to_dying():
    _, state = _table(players=3, rules=COMMANDER)
    _perm(state, 0, bear(1))
    state.player(2).life = 5
    _declare(state, Declaring.ATTACKERS, 0)
    assert _choose(state, 0).target_seat == 2


def test_it_sends_everything_at_an_opponent_it_has_lethal_on():
    _, state = _table()
    for i in range(3):
        _perm(state, 0, bear(i))
    _perm(state, 1, creature("Ogre", 3, 3))
    state.player(1).life = 4
    view = player_view(state, 0)
    assert lethal_on_board(view, 1)
    state.player(1).life = 5
    assert not lethal_on_board(player_view(state, 0), 1)


def test_it_ends_the_declaration_when_no_attack_pays():
    _, state = _table()
    _perm(state, 0, bear(1))
    _perm(state, 1, creature("Ogre", 3, 3))
    _declare(state, Declaring.ATTACKERS, 0)
    assert _choose(state, 0).kind == ActionKind.END_ATTACKERS


def test_it_blocks_to_kill_and_survive():
    _, state = _table()
    attacker = _perm(state, 0, bear(1))
    _perm(state, 1, creature("Ogre", 3, 3))
    _attack(state, attacker, 1)
    choice = _choose(state, 1)
    assert (choice.kind, choice.target_uid) == (ActionKind.DECLARE_BLOCKER, attacker.id)


def test_it_chumps_only_when_the_damage_would_kill_it():
    _, state = _table()
    attacker = _perm(state, 0, creature("Giant", 8, 8))
    _perm(state, 1, bear(1))
    _attack(state, attacker, 1)
    assert _choose(state, 1).kind == ActionKind.END_BLOCKERS
    state.player(1).life = 8
    assert incoming_damage(player_view(state, 1)) == 8
    assert _choose(state, 1).kind == ActionKind.DECLARE_BLOCKER


def test_it_trades_when_the_blocker_is_worth_no_more():
    _, state = _table()
    attacker = _perm(state, 0, creature("Raider", 3, 2))
    _perm(state, 1, creature("Soldier", 2, 2))
    _attack(state, attacker, 1)
    assert _choose(state, 1).kind == ActionKind.DECLARE_BLOCKER


def test_it_never_blocks_menace_alone_and_takes_back_a_lone_block():
    _, state = _table()
    brute = _perm(state, 0, creature("Brute", 2, 2, keywords=["menace"]))
    blocker = _perm(state, 1, creature("Ogre", 3, 3))
    _attack(state, brute, 1)
    assert _choose(state, 1).kind == ActionKind.END_BLOCKERS
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, blocker.id, target_uid=brute.id))
    choice = _choose(state, 1)
    assert (choice.kind, choice.card_uid) == (ActionKind.UNDO_BLOCKER, blocker.id)


def test_block_outcome_accounts_for_first_strike_and_deathtouch():
    knight = {"power": 2, "toughness": 2, "keywords": ["first strike"]}
    bear_ = {"power": 2, "toughness": 2}
    viper = {"power": 1, "toughness": 1, "keywords": ["deathtouch"]}
    wall = {"power": 0, "toughness": 9, "keywords": ["indestructible"]}
    assert block_outcome(bear_, knight) == (False, True)
    assert block_outcome(viper, bear_) == (True, True)
    assert block_outcome(wall, viper) == (False, False)


def test_the_helpers_do_not_touch_the_view():
    _, state = _table()
    _perm(state, 0, bear(1))
    _perm(state, 1, bear(2))
    view = player_view(state, 0)
    before = copy.deepcopy(view)
    for perm in permanents_by_id(view).values():
        creature_value(perm)
        attack_value(view, perm, 1)
    lethal_on_board(view, 1)
    incoming_damage(view)
    assert view == before


def test_it_is_deterministic():
    _, state = _table()
    for i in range(3):
        _perm(state, 0, bear(i))
    _declare(state, Declaring.ATTACKERS, 0)
    picks = {_choose(state, 0) for _ in range(5)}
    assert len(picks) == 1
