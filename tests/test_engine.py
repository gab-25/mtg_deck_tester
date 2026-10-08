"""Tests for the rules kernel."""

import copy

import pytest

from playtest.agents.base import Decision
from playtest.agents.heuristic import HeuristicAgent
from playtest.agents.random_agent import RandomAgent
from playtest.engine.actions import (
    CLOSING_KINDS,
    Action,
    ActionKind,
    commander_cost,
    legal_actions,
)
from playtest.engine.cards import CardSpec, Kind
from playtest.engine.events import EventKind
from playtest.engine.game import EndReason, Game
from playtest.engine.limits import Limits
from playtest.engine.manacost import parse_mana_cost
from playtest.engine.rules import (
    IllegalAction,
    apply,
    check_state_based_actions,
    combat_damage,
    draw,
    has_first_strike_step,
    lose,
    resolve_top,
)
from playtest.engine.state import (
    Card,
    Declaring,
    GameRules,
    Permanent,
    Step,
    assert_invariants,
)
from playtest.engine.view import player_view

from .factories import FOREST, ISLAND, bear, creature, engine_deck

COMMANDER = GameRules(starting_life=40, commander_damage_limit=21)
DUEL = GameRules(starting_life=20, commander_damage_limit=None)

BOLT = CardSpec("Bolt", Kind.SPELL, 1, cost=parse_mana_cost("{R}"), types=("Instant",))
GIANT_GROWTH = CardSpec("Giant Growth", Kind.SPELL, 1, cost=parse_mana_cost("{G}"), types=("Instant",))
DIVINATION = CardSpec("Divination", Kind.SPELL, 3, cost=parse_mana_cost("{2}{U}"), types=("Sorcery",))
MOUNTAIN = CardSpec("Mountain", Kind.LAND, produced_mana=("R",))


class Passive:
    """Always takes the first option: passes, ends declarations."""

    def __init__(self):
        self.calls = 0

    def choose(self, view, options):
        self.calls += 1
        return Decision(options[0])


class Scripted:
    """Picks the first offered action of each kind in ``preference``, else the first option."""

    def __init__(self, *preference):
        self.preference = preference

    def choose(self, view, options):
        for kind in self.preference:
            for action in options:
                if action.kind == kind:
                    return Decision(action)
        return Decision(options[0])


def _game(decks=None, agents=None, rules=DUEL, **kwargs):
    decks = decks or [engine_deck("A"), engine_deck("B")]
    agents = agents or [RandomAgent(i) for i in range(len(decks))]
    kwargs.setdefault("check_invariants", True)
    return Game(decks, agents, rules, seed=kwargs.pop("seed", 1), **kwargs)


def _table(players=2, rules=DUEL, agents=None):
    """A game whose players start with empty hands and battlefields, at seat 0's main phase."""
    decks = [engine_deck(name) for name in "ABCD"[:players]]
    game = _game(decks=decks, rules=rules, agents=agents or [Passive() for _ in decks])
    state = game.state
    for player in state.players:
        player.library += player.hand
        player.hand = []
    state.round = state.turn = 1
    state.active_seat = 0
    state.step = Step.MAIN1
    return game, state


def _card(state, seat, spec, zone="hand", **fields) -> Card:
    card = Card(state.new_id(), spec, seat, **fields)
    state.total_cards += 1
    getattr(state.player(seat), zone).append(card)
    return card


def _perm(state, seat, spec, *, sick=False, **fields) -> Permanent:
    card = Card(state.new_id(), spec, seat, is_commander=fields.pop("is_commander", False))
    state.total_cards += 1
    perm = Permanent(state.new_id(), card, seat, sick=sick, **fields)
    state.player(seat).battlefield.append(perm)
    return perm


def _kinds(events):
    return [e.event.kind if hasattr(e, "event") else e.kind for e in events]


def _of(actions, kind):
    return [a for a in actions if a.kind == kind]


# -- setup ---------------------------------------------------------------------


def test_setup_deals_seven_and_puts_the_commander_in_the_command_zone():
    game = _game()
    # Hands are dealt by run(): the library starts full.
    for player in game.state.players:
        assert len(player.library) == 99
        assert [c.name for c in player.command] == [f"{player.deck_name} Commander"]
        assert player.life == 20


@pytest.mark.parametrize("seed", range(8))
def test_opening_hands_keep_two_to_five_lands_or_mulligan_twice(seed):
    game = _game(agents=[Passive(), Passive()], max_rounds=0, seed=seed)
    game.run()
    for player in game.state.players:
        lands = sum(1 for c in player.hand if c.spec.kind == Kind.LAND)
        assert 2 <= lands <= 5 or player.mulligans == 2
        assert len(player.hand) == 7 - player.mulligans
        assert len(player.hand) + len(player.library) == 99


def test_a_hand_of_seven_lands_is_mulliganed():
    lands_first = [FOREST] * 60 + [bear(i) for i in range(39)]

    class Rigged(Game):
        def _setup(self):
            state = super()._setup()
            # Seven lands on top for seat 0: a hand nobody keeps.
            for player in state.players:
                player.library.sort(key=lambda c: c.spec.kind != Kind.LAND)
            return state

    game = Rigged(
        [engine_deck("A", library=lands_first), engine_deck("B", library=lands_first)],
        [Passive(), Passive()],
        DUEL,
        seed=3,
        max_rounds=1,
    )
    game.run()
    mulligans = [e.event for e in game.events if e.event.kind == EventKind.MULLIGAN]
    assert {e.seat for e in mulligans} == {0, 1}
    for event in mulligans:
        assert event.payload["kept"] == 7 - event.payload["mulligans"]


def test_the_first_mulligan_is_free_with_three_players():
    lands_only = [FOREST] * 99

    def run(players):
        decks = [engine_deck(n, library=lands_only) for n in "ABC"[:players]]
        rules = COMMANDER if players > 2 else DUEL
        game = Game(decks, [Passive() for _ in decks], rules, seed=1, max_rounds=1)
        game.run()
        return [e.event.payload for e in game.events if e.event.kind == EventKind.MULLIGAN]

    # All lands: they mulligan twice and keep what they get.
    assert {p["kept"] for p in run(2)} == {5}
    assert {p["kept"] for p in run(3)} == {6}


def test_the_same_seed_replays_the_same_game():
    def log(seed):
        result = _game(seed=seed, agents=[HeuristicAgent(), HeuristicAgent()], max_rounds=15).run()
        return [(e.seq, e.step, e.event.kind, e.event.seat, e.event.text, e.event.payload) for e in result.events]

    assert log(42) == log(42)
    assert log(42) != log(43)


# -- lands, timing, casting ----------------------------------------------------


def test_one_land_per_turn_at_sorcery_speed():
    _, state = _table()
    first = _card(state, 0, FOREST)
    _card(state, 0, ISLAND)
    lands = _of(legal_actions(state, 0), ActionKind.PLAY_LAND)
    assert [a.card_name for a in lands] == ["Forest", "Island"]

    apply(state, 0, lands[0])
    assert state.player(0).battlefield[-1].card is first
    assert not _of(legal_actions(state, 0), ActionKind.PLAY_LAND)
    with pytest.raises(IllegalAction):
        apply(state, 0, lands[1])


@pytest.mark.parametrize(
    "step, active, stack",
    [(Step.UPKEEP, 0, False), (Step.MAIN1, 1, False), (Step.MAIN2, 0, True), (Step.END_STEP, 0, False)],
)
def test_lands_and_sorceries_wait_for_your_main_phase_and_an_empty_stack(step, active, stack):
    _, state = _table()
    _card(state, 0, FOREST)
    _card(state, 0, bear())
    _perm(state, 0, FOREST)
    _perm(state, 0, FOREST)
    state.step = step
    state.active_seat = active
    if stack:
        _card(state, 1, bear(9), zone="hand")
        state.stack.append(_stack_item(state, 1))
    assert [a.kind for a in legal_actions(state, 0)] == [ActionKind.PASS]


def _stack_item(state, seat):
    from playtest.engine.state import StackItem

    card = state.player(seat).hand.pop()
    return StackItem(state.new_id(), card, seat)


def test_instants_can_be_cast_whenever_you_have_priority():
    _, state = _table()
    _card(state, 1, GIANT_GROWTH)
    _perm(state, 1, FOREST)
    state.step = Step.BEGIN_COMBAT
    [_pass, cast] = legal_actions(state, 1)
    assert cast.kind == ActionKind.CAST_SPELL and cast.card_name == "Giant Growth"


def test_casting_puts_the_spell_on_the_stack_and_it_resolves_later():
    _, state = _table()
    _card(state, 0, bear())
    _perm(state, 0, FOREST)
    _perm(state, 0, FOREST)
    [cast] = _of(legal_actions(state, 0), ActionKind.CAST_SPELL)
    events = apply(state, 0, cast)
    assert events[0].kind == EventKind.CAST
    assert [i.card.name for i in state.stack] == ["Bear 0"]
    assert all(p.tapped for p in state.player(0).battlefield)
    # Nothing at sorcery speed while the spell waits on the stack.
    assert legal_actions(state, 0) == [legal_actions(state, 0)[0]]

    resolve_top(state)
    creature_perm = state.player(0).battlefield[-1]
    assert creature_perm.name == "Bear 0" and creature_perm.sick


def test_a_spell_resolves_only_after_every_living_player_passes():
    asked = []

    class Recorder:
        def __init__(self, seat, cast=False):
            self.seat, self.cast = seat, cast

        def choose(self, view, options):
            asked.append((self.seat, [s["name"] for s in view["stack"]]))
            if self.cast:
                casts = _of(options, ActionKind.CAST_SPELL)
                if casts:
                    return Decision(casts[0])
            return Decision(options[0])

    game, state = _table(players=3, rules=COMMANDER, agents=[Recorder(0, cast=True), Recorder(1), Recorder(2)])
    _card(state, 0, bear())
    _perm(state, 0, FOREST)
    _perm(state, 0, FOREST)
    # Seats 1 and 2 hold an instant, so they are really asked rather than auto-passed.
    for seat in (1, 2):
        _card(state, seat, GIANT_GROWTH)
        _perm(state, seat, FOREST)
    game._priority()

    # Seat 0 gets priority back after casting, but with nothing left to do it passes
    # automatically; then each opponent is asked in turn order before the bear resolves.
    with_bear_on_stack = [seat for seat, stack in asked if stack == ["Bear 0"]]
    assert with_bear_on_stack == [1, 2]
    assert state.player(0).battlefield[-1].name == "Bear 0"
    assert not state.stack


def test_the_engine_auto_passes_when_passing_is_the_only_option():
    uncastable = [CardSpec(f"Huge {i}", Kind.CREATURE, 9, 9, 9, cost=parse_mana_cost("{9}")) for i in range(99)]
    agents = [Passive(), Passive()]
    _game(decks=[engine_deck("A", library=uncastable), engine_deck("B", library=uncastable)], agents=agents, max_rounds=3).run()
    assert [a.calls for a in agents] == [0, 0]


def test_commander_tax_grows_with_each_cast():
    _, state = _table()
    player = state.player(0)
    for _ in range(6):
        _perm(state, 0, FOREST)
    assert commander_cost(state, 0) == 4
    [cast] = _of(legal_actions(state, 0), ActionKind.CAST_COMMANDER)
    apply(state, 0, cast)
    resolve_top(state)
    assert commander_cost(state, 0) is None

    commander = next(p for p in player.battlefield if p.card.is_commander)
    commander.damage = commander.toughness
    events, _ = check_state_based_actions(state)
    assert [e.kind for e in events] == [EventKind.DIES, EventKind.ZONE_CHANGE]
    assert [c.name for c in player.command] == ["A Commander"]
    assert commander_cost(state, 0) == 6
    # Six lands, all untapped again: 4 + 2 tax is castable, and says so.
    for perm in player.battlefield:
        perm.tapped = False
    [cast] = _of(legal_actions(state, 0), ActionKind.CAST_COMMANDER)
    assert "+ 2 tax" in cast.label


def test_x_spells_are_offered_once_per_affordable_x():
    _, state = _table()
    fireball = CardSpec("Fireball", Kind.SPELL, 1, cost=parse_mana_cost("{X}{R}"), types=("Sorcery",))
    _card(state, 0, fireball)
    for _ in range(3):
        _perm(state, 0, MOUNTAIN)
    casts = _of(legal_actions(state, 0), ActionKind.CAST_SPELL)
    assert [a.x for a in casts] == [0, 1, 2]


def test_the_stack_depth_limit_stops_casting():
    _, state = _table()
    _card(state, 1, GIANT_GROWTH)
    _perm(state, 1, FOREST)
    state.limits = Limits(max_stack_depth=1)
    _card(state, 0, bear(7))
    state.stack.append(_stack_item(state, 0))
    assert [a.kind for a in legal_actions(state, 1)] == [ActionKind.PASS]


# -- combat --------------------------------------------------------------------


def _declare(state, kind, seat):
    state.declaring = kind
    state.declaring_seat = seat
    return legal_actions(state, seat)


def test_summoning_sick_creatures_cannot_attack_unless_they_have_haste():
    _, state = _table()
    _perm(state, 0, bear(1), sick=True)
    hasty = _perm(state, 0, creature("Raging Goblin", 1, keywords=["haste"]), sick=True)
    wall = _perm(state, 0, creature("Wall", 0, 4, keywords=["defender"]))
    options = _declare(state, Declaring.ATTACKERS, 0)
    assert options[0].kind == ActionKind.END_ATTACKERS
    assert [a.card_uid for a in options[1:]] == [hasty.id]
    assert wall.id not in [a.card_uid for a in options]


def test_attacking_taps_unless_vigilance():
    _, state = _table()
    plain = _perm(state, 0, bear(1))
    watchful = _perm(state, 0, creature("Sentry", 2, keywords=["vigilance"]))
    _declare(state, Declaring.ATTACKERS, 0)
    for perm in (plain, watchful):
        apply(state, 0, Action(ActionKind.DECLARE_ATTACKER, perm.id, target_seat=1))
    [event] = apply(state, 0, _declare(state, Declaring.ATTACKERS, 0)[0])
    assert event.kind == EventKind.ATTACK
    assert plain.tapped and not watchful.tapped


def _attack(state, attacker, seat):
    state.declaring, state.declaring_seat = Declaring.ATTACKERS, state.active_seat
    apply(state, state.active_seat, Action(ActionKind.DECLARE_ATTACKER, attacker.id, target_seat=seat))
    apply(state, state.active_seat, legal_actions(state, state.active_seat)[0])
    state.declaring, state.declaring_seat = Declaring.BLOCKERS, seat


def test_flying_is_blocked_only_by_flying_or_reach():
    _, state = _table()
    bird = _perm(state, 0, creature("Bird", 1, keywords=["flying"]))
    _perm(state, 1, bear(1))
    spider = _perm(state, 1, creature("Spider", 1, 3, keywords=["reach"]))
    drake = _perm(state, 1, creature("Drake", 2, keywords=["flying"]))
    _attack(state, bird, 1)
    blocks = _of(legal_actions(state, 1), ActionKind.DECLARE_BLOCKER)
    assert sorted(a.card_uid for a in blocks) == sorted([spider.id, drake.id])


def test_menace_needs_two_blockers():
    _, state = _table()
    brute = _perm(state, 0, creature("Brute", 3, keywords=["menace"]))
    first = _perm(state, 1, bear(1))
    second = _perm(state, 1, bear(2))
    _attack(state, brute, 1)
    assert legal_actions(state, 1)[0].kind == ActionKind.END_BLOCKERS

    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, first.id, target_uid=brute.id))
    options = legal_actions(state, 1)
    assert ActionKind.END_BLOCKERS not in [a.kind for a in options]
    assert [a.card_uid for a in _of(options, ActionKind.UNDO_BLOCKER)] == [first.id]

    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, second.id, target_uid=brute.id))
    assert legal_actions(state, 1)[0].kind == ActionKind.END_BLOCKERS


def test_the_declaration_limit_drops_a_lone_menace_block():
    class Dither:
        """Blocks the menace creature with one creature, takes it back, forever."""

        def choose(self, view, options):
            return Decision(next(o for o in options if o.kind not in CLOSING_KINDS))

    game, state = _table(agents=[Passive(), Dither()])
    brute = _perm(state, 0, creature("Brute", 3, keywords=["menace"]))
    _perm(state, 1, bear(1))
    _attack(state, brute, 1)
    game._declare(Declaring.BLOCKERS, 1)
    assert EventKind.LIMIT in _kinds(game.events)
    assert not state.blockers_of(brute.id)
    assert state.declaring is None


def test_lethal_damage_kills_before_anyone_gets_priority():
    seen = []

    class Watcher:
        def choose(self, view, options):
            seen.append([p["name"] for p in view["you"]["battlefield"]])
            return Decision(options[0])

    game, state = _table(agents=[Watcher(), Watcher()])
    attacker = _perm(state, 0, creature("Ogre", 3))
    blocker = _perm(state, 1, bear(1))
    _card(state, 1, GIANT_GROWTH)
    _perm(state, 1, FOREST)  # seat 1 holds an instant, so it is asked.
    _attack(state, attacker, 1)
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, blocker.id, target_uid=attacker.id))
    apply(state, 1, legal_actions(state, 1)[0])
    state.declaring = None
    state.step = Step.COMBAT_DAMAGE
    game._record(combat_damage(state, Step.COMBAT_DAMAGE))
    game._priority()
    assert seen and all("Bear 1" not in names for names in seen)
    assert [c.name for c in state.player(1).graveyard] == ["Bear 1"]


def test_first_strike_deals_damage_in_a_separate_earlier_step():
    _, state = _table()
    knight = _perm(state, 0, creature("Knight", 2, keywords=["first strike"]))
    blocker = _perm(state, 1, bear(1))
    _attack(state, knight, 1)
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, blocker.id, target_uid=knight.id))
    apply(state, 1, legal_actions(state, 1)[0])
    state.declaring = None

    assert has_first_strike_step(state)
    first = combat_damage(state, Step.FIRST_STRIKE_DAMAGE)
    assert [e.payload["source"] for e in first] == ["Knight"]
    check_state_based_actions(state)
    assert blocker not in state.player(1).battlefield
    # The blocker died before it could strike back.
    assert combat_damage(state, Step.COMBAT_DAMAGE) == []
    assert knight.damage == 0


def test_double_strike_deals_damage_in_both_steps():
    _, state = _table()
    duelist = _perm(state, 0, creature("Duelist", 2, keywords=["double strike"]))
    _attack(state, duelist, 1)
    apply(state, 1, legal_actions(state, 1)[0])
    combat_damage(state, Step.FIRST_STRIKE_DAMAGE)
    combat_damage(state, Step.COMBAT_DAMAGE)
    assert state.player(1).life == 16


def test_trample_assigns_only_the_excess_to_the_player():
    _, state = _table()
    wurm = _perm(state, 0, creature("Wurm", 5, keywords=["trample"]))
    blocker = _perm(state, 1, bear(1))
    _attack(state, wurm, 1)
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, blocker.id, target_uid=wurm.id))
    apply(state, 1, legal_actions(state, 1)[0])
    combat_damage(state, Step.COMBAT_DAMAGE)
    assert blocker.damage == 2
    assert state.player(1).life == 17


def test_a_blocked_attacker_without_trample_deals_no_damage_to_the_player():
    _, state = _table()
    ogre = _perm(state, 0, creature("Ogre", 5))
    blocker = _perm(state, 1, bear(1))
    _attack(state, ogre, 1)
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, blocker.id, target_uid=ogre.id))
    apply(state, 1, legal_actions(state, 1)[0])
    combat_damage(state, Step.COMBAT_DAMAGE)
    assert state.player(1).life == 20
    assert blocker.damage == 5


def test_deathtouch_lifelink_and_indestructible():
    _, state = _table()
    viper = _perm(state, 0, creature("Viper", 1, keywords=["deathtouch", "lifelink"]))
    golem = _perm(state, 1, creature("Golem", 4, 4, keywords=["indestructible"]))
    elephant = _perm(state, 1, creature("Elephant", 3, 3))
    _attack(state, viper, 1)
    apply(state, 1, Action(ActionKind.DECLARE_BLOCKER, elephant.id, target_uid=viper.id))
    apply(state, 1, legal_actions(state, 1)[0])
    combat_damage(state, Step.COMBAT_DAMAGE)
    golem.damage = 10
    check_state_based_actions(state)
    assert state.player(0).life == 21
    assert elephant not in state.player(1).battlefield
    assert golem in state.player(1).battlefield
    assert viper not in state.player(0).battlefield


def test_four_players_split_attacks_and_block_only_what_attacks_them():
    _, state = _table(players=4, rules=COMMANDER)
    first = _perm(state, 0, bear(1))
    second = _perm(state, 0, bear(2))
    for seat in (1, 2, 3):
        _perm(state, seat, bear(10 + seat))
    options = _declare(state, Declaring.ATTACKERS, 0)
    # Every ready creature, at every opponent: a sum, not the subsets.
    assert len(_of(options, ActionKind.DECLARE_ATTACKER)) == 2 * 3
    apply(state, 0, Action(ActionKind.DECLARE_ATTACKER, first.id, target_seat=1))
    apply(state, 0, Action(ActionKind.DECLARE_ATTACKER, second.id, target_seat=3))
    apply(state, 0, legal_actions(state, 0)[0])

    for seat, attacker in ((1, first), (3, second)):
        blocks = _of(_declare(state, Declaring.BLOCKERS, seat), ActionKind.DECLARE_BLOCKER)
        assert {a.target_uid for a in blocks} == {attacker.id}
    assert _declare(state, Declaring.BLOCKERS, 2) == [legal_actions(state, 2)[0]]


def test_an_eliminated_players_permanents_and_spells_leave_the_game():
    _, state = _table(players=4, rules=COMMANDER)
    doomed = state.player(2)
    _perm(state, 2, bear(1))
    _perm(state, 2, FOREST)
    _card(state, 2, bear(2))
    state.stack.append(_stack_item(state, 2))
    attacker = _perm(state, 0, bear(3))
    attacker.attacking = 2
    events = lose(state, 2, "testing")
    assert events[0].kind == EventKind.ELIMINATED
    assert not doomed.battlefield and not state.stack
    assert sorted(c.name for c in doomed.out_of_game) == ["Bear 1", "Bear 2", "Forest"]
    assert attacker.attacking is None
    assert_invariants(state)


def test_a_player_who_loses_on_their_own_turn_ends_that_turn():
    class Concede:
        """Seat 0 loses in its first main phase (by life), from inside a decision."""

        def __init__(self, state_ref):
            self.state_ref = state_ref

        def choose(self, view, options):
            self.state_ref[0].player(0).life = 0
            return Decision(options[0])

    ref = []
    agents = [Concede(ref), Passive(), Passive()]
    decks = [engine_deck(n) for n in "ABC"]
    game = Game(decks, agents, COMMANDER, seed=5, max_rounds=2, check_invariants=True)
    ref.append(game.state)
    game.run()
    out = next(e for e in game.events if e.event.kind == EventKind.ELIMINATED)
    assert out.event.seat == 0
    following = game.events[out.seq]
    assert following.event.kind in (EventKind.TURN, EventKind.DISCARD)


# -- state-based actions -------------------------------------------------------


def test_drawing_from_an_empty_library_loses_at_the_next_check():
    _, state = _table()
    state.player(0).library = []
    state.total_cards = sum(
        len(z) for p in state.players for z in p.zones()
    ) + sum(1 for _ in state.permanents())
    [event] = draw(state, 0)
    assert event.payload == {"empty_library": True}
    assert state.player(0).alive  # not yet: the flag is set at draw time
    events, _ = check_state_based_actions(state)
    assert not state.player(0).alive
    assert "empty library" in events[-1].text


def test_commander_damage_at_the_limit_ends_a_players_game():
    _, state = _table(players=3, rules=COMMANDER)
    giant = _perm(state, 0, creature("Giant", 11, legendary=True), is_commander=True)
    state.commanders[giant.card.id] = 0
    for _ in range(2):
        _attack(state, giant, 1)
        apply(state, 1, legal_actions(state, 1)[0])
        combat_damage(state, Step.COMBAT_DAMAGE)
        check_state_based_actions(state)
        giant.attacking = None
        giant.blocked = False
        giant.tapped = False
    victim = state.player(1)
    assert victim.life == 18  # 40 - 22: alive on life, out on commander damage
    assert not victim.alive


def test_the_legend_rule_keeps_the_older_permanent():
    _, state = _table()
    legend = creature("Legend", 2, legendary=True)
    older = _perm(state, 0, legend)
    newer = _perm(state, 0, legend)
    events, _ = check_state_based_actions(state)
    assert older in state.player(0).battlefield and newer not in state.player(0).battlefield
    assert events[0].payload["reason"] == "legend rule"


def test_a_creature_returning_to_the_battlefield_is_a_new_object():
    _, state = _table()
    _card(state, 0, bear(1))
    _perm(state, 0, FOREST)
    _perm(state, 0, FOREST)
    apply(state, 0, _of(legal_actions(state, 0), ActionKind.CAST_SPELL)[0])
    resolve_top(state)
    first = state.player(0).battlefield[-1]
    first.damage = 2
    check_state_based_actions(state)
    card = state.player(0).graveyard.pop()
    state.player(0).hand.append(card)
    for perm in state.player(0).battlefield:
        perm.tapped = False
    apply(state, 0, _of(legal_actions(state, 0), ActionKind.CAST_SPELL)[0])
    resolve_top(state)
    second = state.player(0).battlefield[-1]
    assert second.card is first.card
    assert second.id != first.id
    assert second.damage == 0 and second.sick


# -- limits --------------------------------------------------------------------


def test_passive_players_draw_at_the_round_limit():
    result = _game(agents=[Passive(), Passive()], max_rounds=3).run()
    assert result.end_reason == EndReason.TURN_LIMIT
    assert result.winner_seat is None
    assert _kinds(result.events)[-1] == EventKind.GAME_OVER


def test_the_decision_limit_ends_the_turn_with_an_event():
    class Undecided:
        """Always picks the last option: never passes while anything else is offered."""

        def choose(self, view, options):
            return Decision(options[-1])

    game = _game(agents=[Undecided(), Undecided()], max_rounds=6, max_decisions_per_turn=3)
    game.run()
    limits = [e for e in game.events if e.event.kind == EventKind.DECISION_LIMIT]
    assert limits
    after = game.events[limits[0].seq]
    assert after.step == "cleanup" or after.event.kind == EventKind.TURN


def test_no_decision_offers_more_than_the_option_limit():
    sizes = []

    class Counting(Passive):
        def choose(self, view, options):
            sizes.append(len(options))
            return Decision(options[0])

    game, state = _table(players=3, rules=COMMANDER, agents=[Counting(), Counting(), Counting()])
    for i in range(130):
        _perm(state, 0, bear(i))
    game._declare(Declaring.ATTACKERS, 0)
    assert sizes == [255]
    assert EventKind.LIMIT in _kinds(game.events)


def test_an_action_that_was_not_offered_is_rejected():
    class Cheater:
        def choose(self, view, options):
            return Decision(Action(ActionKind.CAST_COMMANDER, 1))

    game, state = _table(agents=[Cheater(), Cheater()])
    _card(state, 0, FOREST)
    with pytest.raises(IllegalAction):
        game._priority()


def test_reasoning_is_recorded_on_the_log():
    class Chatty:
        def choose(self, view, options):
            return Decision(options[0], reasoning="nothing worth doing")

    game = _game(agents=[Chatty(), Chatty()], max_rounds=1)
    game.run()
    assert any(e.event.reasoning == "nothing worth doing" for e in game.events)


# -- the legal_actions contract ------------------------------------------------

KITCHEN_SINK = (
    [FOREST] * 20
    + [ISLAND] * 8
    + [MOUNTAIN] * 8
    + [CardSpec("Sol Ring", Kind.PERMANENT, 1, cost=parse_mana_cost("{1}"), produced_mana=("C",), mana_amount=2, types=("Artifact",))]
    + [CardSpec("Fireball", Kind.SPELL, 1, cost=parse_mana_cost("{X}{R}"), types=("Sorcery",))]
    + [BOLT, GIANT_GROWTH, DIVINATION]
    + [creature(f"Bird {i}", 1, keywords=["flying"]) for i in range(5)]
    + [creature(f"Brute {i}", 3, keywords=["menace"], mana_value=3) for i in range(5)]
    + [creature(f"Knight {i}", 2, keywords=["first strike"]) for i in range(5)]
    + [creature(f"Wurm {i}", 5, keywords=["trample"], mana_value=5) for i in range(5)]
    + [creature(f"Viper {i}", 1, keywords=["deathtouch", "lifelink"], mana_value=1) for i in range(5)]
    + [creature(f"Raider {i}", 2, keywords=["haste", "vigilance"]) for i in range(5)]
    + [creature(f"Ambusher {i}", 2, keywords=["flash", "reach"]) for i in range(5)]
    + [creature(f"Legend {i}", 3, legendary=True, mana_value=3) for i in range(2)]
    + [bear(i) for i in range(21)]
)
assert len(KITCHEN_SINK) == 99


class Auditor:
    """A random agent that checks the legal_actions contract on every decision it gets."""

    def __init__(self, seed, game_ref):
        self.inner = RandomAgent(seed)
        self.game_ref = game_ref

    def choose(self, view, options):
        state = self.game_ref[0].state
        seat = view["you"]["seat"]
        before = copy.deepcopy(state)
        listed = legal_actions(state, seat)
        assert state == before, "legal_actions changed the state"
        assert listed == legal_actions(state, seat), "legal_actions is not deterministic"
        assert len(options) <= 255
        if state.declaring is None:
            assert listed[0].kind == ActionKind.PASS
        rest = [a for a in listed if a.kind not in CLOSING_KINDS]
        assert rest == sorted(rest, key=Action.sort_key)
        for action in listed:
            trial = copy.deepcopy(state)
            apply(trial, seat, action)
            assert_invariants(trial)
        return self.inner.choose(view, options)


@pytest.mark.parametrize("players, seed", [(2, 1), (3, 2), (4, 3)])
def test_every_listed_action_applies_cleanly_and_invariants_hold(players, seed):
    ref = []
    decks = [engine_deck(n, library=KITCHEN_SINK) for n in "ABCD"[:players]]
    rules = DUEL if players == 2 else COMMANDER
    game = Game(decks, [Auditor(i, ref) for i in range(players)], rules, seed=seed, max_rounds=8, check_invariants=True)
    ref.append(game)
    game.run()
    assert _kinds(game.events)[-1] == EventKind.GAME_OVER


def test_the_view_hides_opponents_hands_and_libraries():
    game = _game()
    state = game.state
    state.player(0).hand = state.player(0).library[:7]
    del state.player(0).library[:7]
    view = player_view(state, 0)
    assert len(view["you"]["hand"]) == 7
    [opponent] = view["opponents"]
    assert opponent["hand_count"] == 0
    assert "hand" not in opponent
    assert "library" not in opponent and "library" not in view["you"]
