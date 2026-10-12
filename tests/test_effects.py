"""Tests for compiled card effects in the kernel: ops, targets, triggers, statics."""

import pytest

from playtest.agents.base import Decision
from playtest.engine.actions import ActionKind, legal_actions
from playtest.engine.cards import card
from playtest.engine.effects import keywords, power, toughness
from playtest.engine.events import EventKind
from playtest.engine.rules import apply, check_state, cleanup_step, resolve_top
from playtest.engine.state import Card
from playtest.engine.zones import enter_battlefield

from .cards import compiled_spec
from .factories import bear
from .test_engine import COMMANDER, _hand, _put, _table

WASTES = card("Wastes", "Basic Land", produces="C")


def _mana(state, seat=0, amount=3):
    for _ in range(amount):
        _put(state, WASTES, seat=seat)


def _cast(state, spec, *, seat=0, targets=None, mode=None):
    """Casts ``spec`` from ``seat``'s hand with the given target refs, and resolves it."""
    item = _hand(state, spec, seat=seat)
    options = [
        a
        for a in legal_actions(state, seat)
        if a.card == item.id and (mode is None or a.mode == mode)
    ]
    if targets is not None:
        options = [a for a in options if [ref for _, ref in a.targets] == targets]
    assert options, f"{spec.name} cannot be cast with {targets}"
    events = apply(state, seat, options[0])
    events += resolve_top(state)
    events += check_state(state)
    return events


def _ref(perm):
    return f"o{perm.id}"


# -- one scenario per operation ------------------------------------------------------


def test_deal_damage_to_a_creature_and_to_a_player():
    game, state = _table()
    _mana(state)
    victim = _put(state, bear(1), seat=1)
    bolt = compiled_spec("Bolt", oracle_text="Bolt deals 3 damage to any target.")
    _cast(state, bolt, targets=[_ref(victim)])
    assert victim not in state.battlefield
    _cast(state, bolt, targets=["p1"])
    assert state.player(1).life == 17


def test_gain_and_lose_life():
    game, state = _table(rules=COMMANDER, players=3)
    _mana(state)
    _cast(state, compiled_spec("Heal", oracle_text="You gain 4 life."))
    _cast(state, compiled_spec("Drain", "Sorcery", oracle_text="Each opponent loses 2 life."))
    assert [p.life for p in state.players] == [44, 38, 38]


def test_draw_discard_and_mill():
    game, state = _table()
    _mana(state, amount=4)
    library = len(state.player(0).library)
    _cast(state, compiled_spec("Think", "Sorcery", oracle_text="Draw two cards."))
    assert len(state.player(0).hand) == 2 and len(state.player(0).library) == library - 2
    for _ in range(3):
        _hand(state, bear(9), seat=1)
    _cast(state, compiled_spec("Rip", "Sorcery", oracle_text="Target player discards two cards."), targets=["p1"])
    assert len(state.player(1).hand) == 1 and len(state.player(1).graveyard) == 2
    before = len(state.player(1).library)
    _cast(state, compiled_spec("Grind", "Sorcery", oracle_text="Target player mills three cards."), targets=["p1"])
    assert len(state.player(1).library) == before - 3


def test_destroy_exile_and_return_to_hand():
    game, state = _table()
    _mana(state, amount=4)
    a, b, c = (_put(state, bear(i), seat=1) for i in range(3))
    _cast(state, compiled_spec("Kill", oracle_text="Destroy target creature."), targets=[_ref(a)])
    _cast(state, compiled_spec("Gone", oracle_text="Exile target creature."), targets=[_ref(b)])
    _cast(state, compiled_spec("Bounce", oracle_text="Return target creature to its owner's hand."), targets=[_ref(c)])
    assert not [p for p in state.battlefield if p.controller == 1]
    assert [x.name for x in state.player(1).hand] == ["Bear 2"]
    assert [x.name for x in state.player(1).exile] == ["Bear 1"]


def test_destroy_spares_indestructible():
    game, state = _table()
    _mana(state)
    wall = _put(state, card("Wall", "Creature", "{2}", 0, 4, keywords=("indestructible",)), seat=1)
    _cast(state, compiled_spec("Kill", oracle_text="Destroy target creature."), targets=[_ref(wall)])
    assert wall in state.battlefield


def test_tap_and_untap():
    game, state = _table()
    _mana(state)
    target = _put(state, bear(1), seat=1)
    _cast(state, compiled_spec("Freeze", oracle_text="Tap target creature."), targets=[_ref(target)])
    assert target.tapped
    mine = _put(state, bear(2), tapped=True)
    _cast(state, compiled_spec("Wake", oracle_text="Untap target creature."), targets=[_ref(mine)])
    assert not mine.tapped


def test_add_mana_pays_for_the_next_spell():
    game, state = _table()
    _put(state, card("Swamp", "Basic Land", produces="B"))
    _cast(state, compiled_spec("Ritual", mana_cost="{B}", oracle_text="Add {B}{B}{B}."))
    assert state.player(0).mana_pool == {"B": 3}
    _hand(state, card("Big", "Creature", "{1}{B}{B}", 4, 4))
    assert any(a.name == "Big" for a in legal_actions(state, 0))


def test_create_tokens_and_tokens_vanish_off_the_battlefield():
    game, state = _table()
    _mana(state, amount=4)
    _cast(state, compiled_spec("Call", "Sorcery", oracle_text="Create two 1/1 white Soldier creature tokens with vigilance."))
    tokens = [p for p in state.battlefield if p.is_token]
    assert len(tokens) == 2 and keywords(state, tokens[0]) == {"vigilance"}
    _cast(state, compiled_spec("Kill", oracle_text="Destroy target creature."), targets=[_ref(tokens[0])])
    assert tokens[0] not in state.battlefield
    assert all(c.name != "Soldier token" for p in state.players for c in p.graveyard)


def test_modify_pt_and_grant_keyword_last_until_end_of_turn():
    game, state = _table()
    _mana(state)
    mine = _put(state, bear(1))
    _cast(state, compiled_spec("Rush", oracle_text="Target creature gets +2/+2 and gains trample until end of turn."), targets=[_ref(mine)])
    assert (power(state, mine), toughness(state, mine)) == (4, 4)
    assert "trample" in keywords(state, mine)
    cleanup_step(state, 0)
    assert (power(state, mine), toughness(state, mine)) == (2, 2)
    assert "trample" not in keywords(state, mine)


def test_put_counters():
    game, state = _table()
    _mana(state)
    mine = _put(state, bear(1))
    _cast(state, compiled_spec("Grow", "Sorcery", oracle_text="Put two +1/+1 counters on target creature."), targets=[_ref(mine)])
    assert (power(state, mine), mine.plus_counters) == (4, 2)


def test_sacrifice_picks_the_least_valuable_creature():
    game, state = _table()
    _mana(state)
    small = _put(state, bear(1, power=1), seat=1)
    big = _put(state, bear(2, power=5), seat=1)
    _cast(state, compiled_spec("Edict", "Sorcery", oracle_text="Each opponent sacrifices a creature."))
    assert small not in state.battlefield and big in state.battlefield


def test_sacrifice_self_as_a_cost_and_effect():
    game, state = _table()
    _mana(state)
    bomb = _put(state, compiled_spec("Bomb", "Artifact", "{1}", "{1}, Sacrifice this artifact: This artifact deals 2 damage to any target."))
    action = next(a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE and a.targets == (("0:t1", "p1"),))
    apply(state, 0, action)
    assert bomb not in state.battlefield
    resolve_top(state)
    assert state.player(1).life == 18


def test_counter_spell():
    game, state = _table()
    _mana(state, seat=0)
    _mana(state, seat=1)
    item = _hand(state, bear(1, cost="{2}"))
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id))
    counter = _hand(state, compiled_spec("No", mana_cost="{2}", oracle_text="Counter target spell."), seat=1)
    action = next(a for a in legal_actions(state, 1) if a.card == counter.id)
    apply(state, 1, action)
    events = resolve_top(state)
    assert any(e.kind == EventKind.COUNTERED for e in events)
    assert not state.stack and [c.name for c in state.player(0).graveyard] == ["Bear 1"]


# -- targets on resolution -----------------------------------------------------------


def test_a_spell_whose_only_target_left_is_countered_on_resolution():
    game, state = _table()
    _mana(state, amount=4)
    victim = _put(state, bear(1), seat=1)
    kill = _hand(state, compiled_spec("Kill", oracle_text="Destroy target creature."))
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == kill.id))
    state.battlefield.remove(victim)
    state.player(1).graveyard.append(victim.card)
    events = resolve_top(state)
    assert [e.kind for e in events] == [EventKind.FIZZLE]
    assert [c.name for c in state.player(0).graveyard] == ["Kill"]


def test_a_partly_illegal_target_set_applies_the_legal_part():
    game, state = _table()
    _mana(state, amount=4)
    victim = _put(state, bear(1), seat=1)
    spell = compiled_spec("Two Shots", "Sorcery", oracle_text="Destroy target creature.\nTarget player loses 2 life.")
    item = _hand(state, spell)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id and dict(a.targets).get("1:t1") == "p1"))
    state.battlefield.remove(victim)
    state.player(1).graveyard.append(victim.card)
    resolve_top(state)
    assert state.player(1).life == 18


def test_restrictions_see_current_power():
    game, state = _table()
    _mana(state, amount=4)
    target = _put(state, bear(1), seat=1)
    snipe = compiled_spec("Snipe", oracle_text="Destroy target creature with power 2 or less.")
    item = _hand(state, snipe)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id))
    target.eot_power = 2  # pumped in response
    events = resolve_top(state)
    assert [e.kind for e in events] == [EventKind.FIZZLE]


def test_hexproof_cannot_be_targeted_by_opponents():
    game, state = _table()
    _mana(state)
    _put(state, card("Shy", "Creature", "{2}", 2, 2, keywords=("hexproof",)), seat=1)
    _hand(state, compiled_spec("Kill", oracle_text="Destroy target creature."))
    assert not [a for a in legal_actions(state, 0) if a.kind == ActionKind.CAST]


def test_ward_is_paid_on_top_of_the_cost():
    game, state = _table()
    _mana(state, amount=2)
    _put(state, card("Warded", "Creature", "{2}", 2, 2, keywords=("ward",), ward=2), seat=1)
    _hand(state, compiled_spec("Kill", oracle_text="Destroy target creature."))
    # {1} + ward {2} needs three mana: not castable with two.
    assert not [a for a in legal_actions(state, 0) if a.kind == ActionKind.CAST]
    _mana(state, amount=1)
    assert [a.label for a in legal_actions(state, 0) if a.kind == ActionKind.CAST][0].startswith("Cast Kill ({3})")


# -- triggers ------------------------------------------------------------------------


def _run_priority(game, agents=None):
    class First:
        def choose(self, view, options):
            return Decision(options[0])

    game.agents = agents or [First() for _ in game.state.players]
    game._priority()


def test_etb_trigger_targets_and_resolves():
    game, state = _table()
    _mana(state, amount=4)
    victim = _put(state, bear(1), seat=1)
    eater = compiled_spec("Eater", "Creature — Horror", "{3}", "When this creature enters, destroy target creature an opponent controls.", "2", "2")
    item = _hand(state, eater)

    class Caster:
        def choose(self, view, options):
            casts = [o for o in options if o.card == item.id]
            if casts:
                return Decision(casts[0])
            targets = [o for o in options if o.kind == ActionKind.CHOOSE_TARGET]
            return Decision(targets[0] if targets else options[0])

    _run_priority(game, [Caster(), Caster()])
    assert victim not in state.battlefield
    assert any(p.name == "Eater" for p in state.battlefield)


def test_simultaneous_triggers_follow_apnap_and_a_trigger_can_trigger():
    game, state = _table(rules=COMMANDER, players=3)
    order = []
    chain = compiled_spec("Chain", "Creature — Imp", "{1}", "When this creature dies, each player loses 1 life.", "1", "1")
    seats = [2, 0, 1]
    perms = {seat: enter_battlefield(state, Card(state.new_id(), chain, seat), seat) for seat in seats}
    state.triggers.clear()
    state.active_seat = 1
    for perm in perms.values():
        perm.damage = 5
    check_state(state)
    # Dies triggers queued for seats 2, 0, 1 go on the stack active player first: 1, 2, 0.
    game._flush_triggers()
    order = [item.controller for item in state.stack]
    assert order == [1, 2, 0]
    # The last one in resolves first.
    resolve_top(state)
    assert [p.life for p in state.players] == [39, 39, 39]


def test_a_trigger_that_causes_a_trigger_resolves_after_it():
    game, state = _table()
    _mana(state, amount=3)
    pinger = compiled_spec("Pinger", "Creature — Goblin", "{1}", "When this creature enters, this creature deals 1 damage to target creature.", "1", "1")
    martyr = compiled_spec("Martyr", "Creature — Cleric", "{1}", "When this creature dies, you gain 3 life.", "1", "1")
    victim = _put(state, martyr, seat=1)
    item = _hand(state, pinger)

    class Caster:
        def choose(self, view, options):
            casts = [o for o in options if o.card == item.id]
            if casts:
                return Decision(casts[0])
            targets = [o for o in options if o.kind == ActionKind.CHOOSE_TARGET and o.ref == f"o{victim.id}"]
            return Decision(targets[0] if targets else options[0])

    _run_priority(game, [Caster(), Caster()])
    kinds = [e.event.kind for e in game.events]
    assert victim not in state.battlefield
    assert state.player(1).life == 23
    # Pinger's trigger, the damage, then the dies trigger and its life gain.
    assert kinds.index(EventKind.DIES) < max(i for i, k in enumerate(kinds) if k == EventKind.TRIGGER)


def test_each_opponent_skips_eliminated_players():
    game, state = _table(rules=COMMANDER, players=4)
    _mana(state)
    state.player(2).life = 0
    check_state(state)
    _cast(state, compiled_spec("Drain", "Sorcery", oracle_text="Each opponent loses 2 life."))
    assert [p.life for p in state.players] == [40, 38, 0, 38]


# -- statics -------------------------------------------------------------------------


def test_anthems_are_a_derived_view():
    game, state = _table()
    mine = _put(state, bear(1))
    theirs = _put(state, bear(2), seat=1)
    anthem = _put(state, compiled_spec("Anthem", "Enchantment", "{2}", "Creatures you control get +1/+1."))
    assert (power(state, mine), power(state, theirs)) == (3, 2)
    assert mine.spec.power == 2
    state.battlefield.remove(anthem)
    assert power(state, mine) == 2


def test_a_dying_anthem_can_kill():
    game, state = _table()
    lord = _put(state, compiled_spec("Lord", "Creature — Elf", "{2}", "Other creatures you control get +1/+1.", "1", "1"))
    weak = _put(state, card("Weak", "Creature", "{1}", 1, 0))
    check_state(state)
    assert weak in state.battlefield
    lord.damage = 1
    check_state(state)
    assert lord not in state.battlefield and weak not in state.battlefield


# -- unsupported cards ------------------------------------------------------------------


def test_an_unsupported_instant_is_never_offered_and_reported_once():
    game, state = _table()
    _mana(state)
    _hand(state, card("Mystery", "Instant", "{1}", program=None))
    assert all(a.kind != ActionKind.CAST for a in legal_actions(state, 0))
    game._report_dead_spells(0)
    game._report_dead_spells(0)
    assert [e.event.kind for e in game.events] == [EventKind.UNSUPPORTED_SKIPPED]


def test_an_unsupported_permanent_enters_as_a_vanilla_body():
    game, state = _table()
    _mana(state)
    odd = card("Odd Beast", "Creature", "{2}", 3, 3, keywords=("trample",), program=None)
    item = _hand(state, odd)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id))
    resolve_top(state)
    perm = next(p for p in state.battlefield if p.name == "Odd Beast")
    assert (power(state, perm), keywords(state, perm)) == (3, {"trample"})


def test_the_view_shows_effect_summaries_not_oracle_text():
    from playtest.engine.view import player_view

    game, state = _table()
    _hand(state, compiled_spec("Bolt", oracle_text="Bolt deals 3 damage to any target."))
    [view_card] = player_view(state, 0)["you"]["hand"]
    assert view_card["effects"][0]["text"] == "deal 3 damage to any target"
    assert "oracle" not in str(view_card).lower()


# -- whole games with effects ----------------------------------------------------------


@pytest.mark.parametrize("players", [2, 4])
def test_heuristic_games_with_real_effects_reach_a_winner(players):
    from playtest.agents.heuristic import HeuristicAgent
    from playtest.engine.game import EndReason, Game

    from .decks import spell_deck
    from .test_engine import DUEL

    decks = [spell_deck(c) for c in "ABCD"[:players]]
    rules = COMMANDER if players > 2 else DUEL
    result = Game(decks, [HeuristicAgent(i) for i in range(players)], rules, seed=9, max_rounds=40, check_invariants=True).run()
    assert result.end_reason == EndReason.LAST_STANDING
    kinds = {e.event.kind for e in result.events}
    assert {EventKind.RESOLVE, EventKind.EFFECT} <= kinds
    assert EventKind.WARNING not in kinds and EventKind.LIMIT not in kinds


@pytest.mark.parametrize("seed", range(3))
def test_random_games_with_real_effects_keep_the_invariants(seed):
    from playtest.agents.random_agent import RandomAgent
    from playtest.engine.game import Game

    from .decks import spell_deck

    players = 2 + seed
    decks = [spell_deck(c) for c in "ABCD"[:players]]
    Game(decks, [RandomAgent(seed + i) for i in range(players)], COMMANDER, seed=seed, max_rounds=20, check_invariants=True).run()
