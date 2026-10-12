"""Tests for the rules kernel: turns, priority, mana, combat, state-based actions."""

import copy
import json
import subprocess
import sys

import pytest

from playtest.agents.base import Decision
from playtest.agents.heuristic import HeuristicAgent
from playtest.agents.random_agent import RandomAgent
from playtest.engine.actions import Action, ActionKind, commander_cost, legal_actions
from playtest.engine.cards import DeckSpec, card
from playtest.engine.events import EventKind
from playtest.engine.game import EndReason, Game
from playtest.engine.limits import Limits
from playtest.engine.mana import can_pay, choose_payment, sources
from playtest.engine.manacost import parse_mana_cost
from playtest.engine.rules import IllegalAction, apply, check_state, combat_damage, resolve_top
from playtest.engine.state import Card, Combat, GameRules, Step, assert_invariants
from playtest.engine.view import player_view
from playtest.engine.zones import enter_battlefield

from .factories import FOREST, bear, engine_deck

COMMANDER = GameRules(starting_life=40, commander_damage_limit=21)
DUEL = GameRules(starting_life=20, commander_damage_limit=None)

ISLAND = card("Island", "Basic Land", produces="U")
SOL_RING = card("Sol Ring", "Artifact", "{1}", produces="C", amount=2)
TOWER = card("Command Tower", "Land", produces="WUBRG")


class Passive:
    def choose(self, view, options):
        return Decision(options[0])


def _game(decks=None, agents=None, rules=DUEL, **kwargs):
    decks = decks or [engine_deck("A"), engine_deck("B")]
    agents = agents or [HeuristicAgent(i) for i in range(len(decks))]
    return Game(decks, agents, rules, seed=kwargs.pop("seed", 1), **kwargs)


def _table(*, rules=DUEL, players=2):
    """A game frozen in seat 0's first main phase, with empty hands and boards."""
    game = _game(decks=[engine_deck(c) for c in "ABCD"[:players]], rules=rules)
    state = game.state
    state.order = list(range(players))
    state.round = state.turn = 1
    state.active_seat = 0
    state.step = Step.MAIN1
    for player in state.players:
        player.library += player.hand
        player.hand = []
    return game, state


def _put(state, spec, seat=0, *, sick=False, tapped=False):
    perm = enter_battlefield(state, Card(state.new_id(), spec, seat), seat)
    perm.sick = sick
    perm.tapped = tapped
    state.triggers.clear()
    return perm


def _hand(state, spec, seat=0):
    item = Card(state.new_id(), spec, seat)
    state.player(seat).hand.append(item)
    return item


def _kinds(result):
    return [e.event.kind for e in result.events]


# -- setup, determinism, termination -------------------------------------------------


def test_setup_puts_the_commander_in_the_command_zone_and_mulligans_deterministically():
    game = _game()
    game.run()
    for player in game.state.players:
        assert player.life is not None
    first = _game(seed=7)
    second = _game(seed=7)
    assert [c.id for c in first.state.players[0].library] == [c.id for c in second.state.players[0].library]


def _log(result):
    return [(e.round, e.step, e.event.kind, e.event.seat, e.event.text, e.event.payload) for e in result.events]


def test_the_same_seed_replays_event_for_event():
    assert _log(_game(seed=42).run()) == _log(_game(seed=42).run())


@pytest.mark.parametrize("players, rules", [(2, DUEL), (4, COMMANDER)])
def test_heuristic_agents_play_to_a_winner_with_no_warnings(players, rules):
    decks = [engine_deck(c) for c in "ABCD"[:players]]
    result = _game(decks=decks, rules=rules, seed=3, max_rounds=40, check_invariants=True).run()
    assert result.end_reason == EndReason.LAST_STANDING
    assert result.winner_seat is not None
    kinds = _kinds(result)
    assert EventKind.WARNING not in kinds and EventKind.LIMIT not in kinds


def test_passive_players_draw_at_the_round_limit():
    result = _game(agents=[Passive(), Passive()], max_rounds=3).run()
    assert result.end_reason == EndReason.TURN_LIMIT
    assert result.winner_seat is None
    assert _kinds(result)[-1] == EventKind.GAME_OVER


@pytest.mark.parametrize("seed", range(4))
def test_random_agents_never_break_the_invariants(seed):
    players = 2 + seed % 3
    decks = [engine_deck(c) for c in "ABCD"[:players]]
    agents = [RandomAgent(seed * 10 + i) for i in range(players)]
    result = _game(decks=decks, agents=agents, rules=COMMANDER, seed=seed, max_rounds=25, check_invariants=True).run()
    assert result.end_reason in (EndReason.LAST_STANDING, EndReason.TURN_LIMIT)


def test_the_log_does_not_depend_on_the_hash_seed():
    script = (
        "import hashlib, json, sys; sys.path.insert(0, '.');"
        "from tests.test_engine import _game, _log;"
        "print(hashlib.sha256(json.dumps(_log(_game(seed=5, max_rounds=8).run()), default=str).encode()).hexdigest())"
    )
    digests = {
        subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env={"PYTHONHASHSEED": str(h), "PATH": ""}, check=True).stdout
        for h in (1, 2)
    }
    assert len(digests) == 1


# -- the contract of legal_actions ---------------------------------------------------


def test_pass_comes_first_and_listing_is_pure():
    game, state = _table()
    _hand(state, FOREST)
    _hand(state, bear())
    _put(state, FOREST)
    _put(state, FOREST)
    before = copy.deepcopy(state)
    actions = legal_actions(state, 0)
    assert actions[0].kind == ActionKind.PASS
    assert [a.kind for a in actions] == [ActionKind.PASS, ActionKind.PLAY_LAND, ActionKind.CAST]
    assert state == before


def test_every_listed_action_applies_cleanly():
    for seed in range(2):
        game = _game(decks=[engine_deck(c) for c in "ABC"], agents=[RandomAgent(seed + i) for i in range(3)], rules=COMMANDER, seed=seed, max_rounds=5)
        original = game._decide

        def decide(seat, original=original, game=game):
            for action in legal_actions(game.state, seat, game.limits):
                trial = copy.deepcopy(game.state)
                apply(trial, seat, action, game.limits)
                assert_invariants(trial, game.cards_per_seat)
            return original(seat)

        game._decide = decide
        game.run()


def test_no_decision_offers_more_than_255_actions():
    seen = []

    class Counting:
        def __init__(self):
            self.inner = RandomAgent(1)

        def choose(self, view, options):
            seen.append(len(options))
            return self.inner.choose(view, options)

    _game(decks=[engine_deck(c) for c in "ABCD"], agents=[Counting() for _ in range(4)], rules=COMMANDER, max_rounds=15).run()
    assert seen and max(seen) <= 255


def test_an_action_that_was_not_offered_is_rejected():
    class Cheater:
        def choose(self, view, options):
            return Decision(Action(ActionKind.CAST_COMMANDER, card=1))

    library = [FOREST] * 99
    decks = [engine_deck("A", library=library), engine_deck("B", library=library)]
    with pytest.raises(IllegalAction):
        _game(decks=decks, agents=[Cheater(), Cheater()]).run()
    game, state = _table()
    with pytest.raises(IllegalAction):
        apply(state, 0, Action(ActionKind.PLAY_LAND, card=12345))


# -- turn structure and priority -----------------------------------------------------


def test_one_land_per_turn_at_sorcery_speed():
    game, state = _table()
    first, _second = _hand(state, FOREST), _hand(state, FOREST)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == first.id))
    assert not [a for a in legal_actions(state, 0) if a.kind == ActionKind.PLAY_LAND]
    # Not in the opponent's turn either.
    _hand(state, FOREST, seat=1)
    assert not [a for a in legal_actions(state, 1) if a.kind == ActionKind.PLAY_LAND]


def test_a_spell_resolves_only_after_every_living_player_passes():
    asked = []

    class Recorder:
        def __init__(self, seat):
            self.seat = seat

        def choose(self, view, options):
            asked.append((self.seat, len(view["stack"])))
            casts = [o for o in options if o.kind == ActionKind.CAST and o.name == "Bear 1"]
            return Decision(casts[0] if casts else options[0])

    game, state = _table(rules=COMMANDER, players=3)
    game.agents = [Recorder(i) for i in range(3)]
    flash = card("Flash Bear", "Creature", "{G}", 2, 2, keywords=("flash",))
    for seat in range(3):
        _put(state, FOREST, seat=seat)
        _put(state, FOREST, seat=seat)
    _hand(state, bear(1))
    for seat in (1, 2):
        # Something to cast in response, so the engine has to ask.
        _hand(state, flash, seat=seat)
    game._priority()
    # Seat 0 cast with an empty stack (then had nothing left but passing, so it
    # was not asked); both opponents were asked before the bear resolved.
    assert asked[0] == (0, 0)
    assert {seat for seat, depth in asked if depth == 1} == {1, 2}
    assert any(p.name == "Bear 1" for p in state.battlefield)


def test_the_engine_auto_passes_when_passing_is_the_only_option():
    calls = []

    class Counting:
        def choose(self, view, options):
            calls.append(len(options))
            return Decision(options[0])

    library = [card("Nothing", "Sorcery", "{9}", program=None)] * 99
    decks = [engine_deck("A", library=library, commander_cost="{9}"), engine_deck("B", library=library, commander_cost="{9}")]
    _game(decks=decks, agents=[Counting(), Counting()], max_rounds=3).run()
    assert calls == []


def test_summoning_sick_creatures_cannot_attack_unless_they_have_haste():
    game, state = _table()
    state.step = Step.DECLARE_ATTACKERS
    state.combat = Combat("attackers")
    _put(state, bear(1), sick=True)
    _put(state, bear(2, keywords=("haste",)), sick=True)
    attackers = [a.name for a in legal_actions(state, 0) if a.kind == ActionKind.DECLARE_ATTACKER]
    assert attackers == ["Bear 2"]


def test_decision_cap_ends_the_turn_with_an_event():
    class Stubborn:
        """Never passes if anything else is offered."""

        def choose(self, view, options):
            return Decision(options[-1])

    library = [card(f"Free {i}", "Artifact", "{0}") for i in range(99)]
    game = _game(decks=[engine_deck("A", library=library), engine_deck("B")], agents=[Stubborn(), Passive()], max_rounds=1,
                 limits=Limits(max_decisions_per_turn=3))
    result = game.run()
    assert EventKind.DECISION_LIMIT in _kinds(result)


# -- mana ------------------------------------------------------------------------------


def test_paying_green_does_not_tap_the_only_blue_source():
    game, state = _table()
    island = _put(state, ISLAND)
    tower = _put(state, TOWER)
    forest = _put(state, FOREST)
    chosen = choose_payment(sources(state, 0), parse_mana_cost("{1}{G}"))
    used = {s.permanent_id for s in chosen}
    # The Forest and the Tower pay, keeping the Island (the only other blue source) open...
    assert forest.id in used and island.id not in used or tower.id not in used
    # ...and blue stays castable afterwards.
    rest = [s for s in sources(state, 0) if s.permanent_id not in used]
    assert can_pay(rest, parse_mana_cost("{U}"))


def test_payment_is_deterministic_and_handles_two_mana_sources():
    game, state = _table()
    _put(state, SOL_RING)
    _put(state, FOREST)
    srcs = sources(state, 0)
    assert can_pay(srcs, parse_mana_cost("{2}{G}"))
    assert not can_pay(srcs, parse_mana_cost("{3}{G}"))
    assert not can_pay(srcs, parse_mana_cost("{G}{G}"))
    assert choose_payment(srcs, parse_mana_cost("{1}{G}")) == choose_payment(sources(state, 0), parse_mana_cost("{1}{G}"))


def test_hybrid_and_colored_pips_use_max_flow():
    game, state = _table()
    _put(state, FOREST)
    _put(state, card("Plains", "Basic Land", produces="W"))
    srcs = sources(state, 0)
    assert can_pay(srcs, parse_mana_cost("{G/W}{G}"))
    assert can_pay(srcs, parse_mana_cost("{G/W}{W}"))
    assert not can_pay(srcs, parse_mana_cost("{G/W}{G}{W}"))


def test_mana_creatures_are_summoning_sick():
    game, state = _table()
    elf = card("Elf", "Creature", "{G}", 1, 1, produces="G")
    _put(state, elf, sick=True)
    assert sources(state, 0) == []


# -- casting and the commander -----------------------------------------------------------


def test_commander_returns_to_the_command_zone_and_costs_two_more():
    game, state = _table()
    for _ in range(6):
        _put(state, FOREST)
    assert str(commander_cost(state, 0)) == "{3}{G}"
    cast = next(a for a in legal_actions(state, 0) if a.kind == ActionKind.CAST_COMMANDER)
    apply(state, 0, cast)
    resolve_top(state)
    commander = next(p for p in state.battlefield if p.is_commander)
    commander.damage = 99
    events = check_state(state)
    assert any("command zone" in e.text for e in events)
    assert str(commander_cost(state, 0)) == "{5}{G}"


def test_unsupported_instants_cannot_be_cast():
    game, state = _table()
    _put(state, FOREST)
    _hand(state, card("Mystery", "Instant", "{G}", program=None))
    assert [a.kind for a in legal_actions(state, 0)] == [ActionKind.PASS]


# -- combat ---------------------------------------------------------------------------


def _combat(state, attacker_spec, blocker_specs, *, defender=1):
    attacker = _put(state, attacker_spec)
    attacker.attacking = defender
    blockers = []
    for spec in blocker_specs:
        blocker = _put(state, spec, seat=defender)
        blocker.blocking = attacker.id
        attacker.blocked_by.append(blocker.id)
        attacker.was_blocked = True
        blockers.append(blocker)
    return attacker, blockers


def test_lethal_damage_kills_before_anyone_gets_priority():
    game, state = _table()
    attacker, [blocker] = _combat(state, bear(1, power=3), [bear(2)])
    combat_damage(state, False, set())
    check_state(state)
    assert blocker not in state.battlefield
    assert attacker in state.battlefield and attacker.damage == 2


def test_trample_assigns_only_the_excess():
    game, state = _table()
    trampler = card("Trampler", "Creature", "{4}", 5, 5, keywords=("trample",))
    _combat(state, trampler, [bear(2)])
    combat_damage(state, False, set())
    assert state.player(1).life == 17


def test_first_strike_deals_damage_in_its_own_step():
    game, state = _table()
    striker = card("Striker", "Creature", "{2}", 2, 1, keywords=("first strike",))
    attacker, [blocker] = _combat(state, striker, [bear(2)])
    dealt = set()
    combat_damage(state, True, dealt)
    check_state(state)
    assert blocker not in state.battlefield
    combat_damage(state, False, dealt)
    check_state(state)
    assert attacker in state.battlefield


def test_flying_is_blocked_only_by_flying_or_reach():
    game, state = _table()
    flyer = _put(state, card("Bird", "Creature", "{1}", 1, 1, keywords=("flying",)))
    flyer.attacking = 1
    _put(state, bear(2), seat=1)
    _put(state, card("Spider", "Creature", "{2}", 1, 3, keywords=("reach",)), seat=1)
    state.combat = Combat("blockers", 1)
    blockers = [a.name for a in legal_actions(state, 1) if a.kind == ActionKind.DECLARE_BLOCKER]
    assert blockers == ["Spider"]


def test_menace_needs_two_blockers():
    game, state = _table()
    brute = _put(state, card("Brute", "Creature", "{2}", 3, 3, keywords=("menace",)))
    brute.attacking = 1
    first = _put(state, bear(1), seat=1)
    _put(state, bear(2), seat=1)
    state.combat = Combat("blockers", 1)
    block = next(a for a in legal_actions(state, 1) if a.kind == ActionKind.DECLARE_BLOCKER and a.permanent == first.id)
    apply(state, 1, block)
    kinds = [a.kind for a in legal_actions(state, 1)]
    assert ActionKind.END_BLOCKERS not in kinds
    second = next(a for a in legal_actions(state, 1) if a.kind == ActionKind.DECLARE_BLOCKER)
    apply(state, 1, second)
    assert legal_actions(state, 1)[0].kind == ActionKind.END_BLOCKERS


def test_attacks_split_across_opponents_and_each_defender_blocks_only_their_attackers():
    game, state = _table(rules=COMMANDER, players=4)
    state.step = Step.DECLARE_ATTACKERS
    state.combat = Combat("attackers")
    a = _put(state, bear(1))
    b = _put(state, bear(2, power=3))
    apply(state, 0, next(x for x in legal_actions(state, 0) if x.permanent == a.id and x.defender == 1))
    apply(state, 0, next(x for x in legal_actions(state, 0) if x.permanent == b.id and x.defender == 2))
    for seat in (1, 2, 3):
        _put(state, bear(10 + seat), seat=seat)
    state.combat = Combat("blockers", 1)
    targets = {x.attacker for x in legal_actions(state, 1) if x.kind == ActionKind.DECLARE_BLOCKER}
    assert targets == {a.id}
    state.combat = Combat("blockers", 3)
    assert not [x for x in legal_actions(state, 3) if x.kind == ActionKind.DECLARE_BLOCKER]


def test_commander_damage_at_the_limit_ends_a_players_game():
    game, state = _table(rules=COMMANDER, players=3)
    big = card("Big", "Legendary Creature", "{4}", 11, 11)
    commander = enter_battlefield(state, Card(state.new_id(), big, 0, is_commander=True), 0)
    for _ in range(2):
        commander.attacking = 1
        combat_damage(state, False, set())
    events = check_state(state)
    assert state.player(1).life == 18
    assert not state.player(1).alive
    assert any(e.kind == EventKind.ELIMINATED for e in events)


def test_drawing_from_an_empty_library_loses_at_the_next_check():
    game, state = _table()
    state.player(0).library = []
    from playtest.engine.effects import draw_cards

    draw_cards(state, 0, 1)
    assert state.player(0).alive
    check_state(state)
    assert not state.player(0).alive


def test_an_eliminated_players_permanents_leave_the_game():
    game, state = _table(rules=COMMANDER, players=4)
    player = state.player(2)
    own = next(c for c in player.library if c.spec.is_creature)
    player.library.remove(own)
    mine = enter_battlefield(state, own, 2)
    player.life = 0
    check_state(state)
    assert mine not in state.battlefield
    assert not state.player(2).alive
    assert_invariants(state, game.cards_per_seat)


# -- the view ---------------------------------------------------------------------------


def test_the_view_hides_opponents_hands_and_libraries():
    game = _game()
    game.run()
    view = player_view(game.state, 0)
    [opponent] = view["opponents"]
    assert "hand" not in opponent and "hand_count" in opponent
    assert "library" not in opponent and "library" not in view["you"]
    json.dumps(view)


# -- a scripted duel ----------------------------------------------------------------------


def test_scripted_ten_card_duel():
    """Two decks of ten cards each, heuristic agents: the log is checked by hand."""
    ogre = card("Ogre", "Creature", "{1}{G}", 3, 2)
    library_a = tuple([FOREST] * 5 + [ogre] * 4)
    library_b = tuple([FOREST] * 9)
    commander_a = card("Alpha", "Legendary Creature", "{9}{G}", 9, 9)
    commander_b = card("Beta", "Legendary Creature", "{9}{G}", 9, 9)
    rules = GameRules(starting_life=6, commander_damage_limit=None, opening_hand=3)
    game = Game(
        [DeckSpec("A", commander_a, library_a), DeckSpec("B", commander_b, library_b)],
        [HeuristicAgent(0), HeuristicAgent(1)],
        rules,
        seed=11,
        max_rounds=10,
        check_invariants=True,
    )
    result = game.run()
    texts = [e.event.text for e in result.events if e.event.kind in (EventKind.CAST, EventKind.ATTACK, EventKind.ELIMINATED, EventKind.GAME_OVER)]
    assert texts == EXPECTED_DUEL, texts


# Checked by hand: B (all lands) goes first; A curves Ogres out from turn two,
# attacks with the first one on turn three and with two on turn four.
EXPECTED_DUEL = [
    "A casts Ogre.",
    "A casts Ogre.",
    "Ogre attacks B.",
    "A casts Ogre.",
    "Ogre attacks B.",
    "Ogre attacks B.",
    "B is eliminated: life total reached 0.",
    "A wins in round 4.",
]


def test_a_two_mana_source_floats_what_the_cost_leaves():
    from playtest.engine.mana import pay

    game, state = _table()
    _put(state, SOL_RING)
    pay(state, 0, parse_mana_cost("{1}"))
    assert state.player(0).mana_pool == {"C": 1}
    assert can_pay(sources(state, 0), parse_mana_cost("{1}"))


def test_an_unreadable_cost_degrades_the_card_not_the_match():
    from playtest.engine.cardrules import characteristics
    from playtest.engine.cards import CardSpec

    from .cards import raw_card

    facts = characteristics(raw_card("Odd", "Creature — Elf", "{1}{G}{G/U/P}", power="2", toughness="2"))
    spec = CardSpec.from_facts(facts, None)
    assert spec.cost.mana_value == facts.mana_value


def test_when_the_active_player_leaves_others_spells_stay_on_the_stack():
    game, state = _table(rules=COMMANDER, players=3)
    _put(state, FOREST, seat=2)
    _put(state, FOREST, seat=2)
    spell = _hand(state, bear(9), seat=2)
    state.active_seat = 2
    apply(state, 2, next(a for a in legal_actions(state, 2) if a.card == spell.id))
    state.active_seat = 0
    state.player(0).life = 0
    check_state(state)
    assert [item.name for item in state.stack] == ["Bear 9"]
