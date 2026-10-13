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
from .test_engine import COMMANDER, DUEL, _hand, _put, _table

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


# -- library searches, scry, surveil, costs with life --------------------------------


FETCH = "{T}, Pay 1 life, Sacrifice this land: Search your library for a Forest or Island card, put it onto the battlefield, then shuffle."


def test_a_fetch_land_finds_the_land_the_agent_picks():
    from playtest.engine.state import Card

    game, state = _table()
    fetch = _put(state, compiled_spec("Fetch", "Land", "", FETCH))
    island = card("Island", "Basic Land", produces="U")
    state.player(0).library.append(Card(state.new_id(), island, 0))
    action = next(a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE)
    events = apply(state, 0, action)
    assert fetch not in state.battlefield and state.player(0).life == 19
    asked = []

    def choose(state_, seat, kind, prompt, options):
        asked.append((kind, [o.label for o in options]))
        return next(o.key for o in options if o.label == "Find Island")

    resolve_top(state, choose)
    assert asked[0][0] == "search" and "Find nothing" in asked[0][1] and "Find Forest" in asked[0][1]
    assert any(p.name == "Island" for p in state.battlefield)


def test_wasteland_destroys_only_nonbasic_lands():
    game, state = _table()
    waste = _put(state, compiled_spec("Waste", "Land", "", "{T}, Sacrifice this land: Destroy target nonbasic land."))
    basic = _put(state, card("Forest", "Basic Land", produces="G"), seat=1)
    dual = _put(state, card("Dual", "Land", produces="UG"), seat=1)
    actions = [a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE]
    # Targets are chosen before the cost is paid, so Wasteland may even target itself.
    targets = {dict(a.targets)["0:t1"] for a in actions}
    assert f"o{dual.id}" in targets and f"o{basic.id}" not in targets
    apply(state, 0, next(a for a in actions if dict(a.targets)["0:t1"] == f"o{dual.id}"))
    resolve_top(state)
    assert dual not in state.battlefield and basic in state.battlefield and waste not in state.battlefield


def test_a_life_cost_needs_enough_life():
    game, state = _table()
    _put(state, compiled_spec("Fetch", "Land", "", FETCH))
    state.player(0).life = 0
    assert not [a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE]


def test_scry_and_surveil_ask_card_by_card():
    game, state = _table()
    _mana(state, amount=2)
    top = [c.name for c in state.player(0).library[:2]]
    answers = iter(["c01", "c00"])
    _hand(state, compiled_spec("Peek", "Sorcery", "{1}", "Surveil 2."))
    item = state.player(0).hand[-1]
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id))
    resolve_top(state, lambda *args: next(answers))
    assert [c.name for c in state.player(0).graveyard][:1] == [top[0]]
    assert state.player(0).library[0].name == top[1]


def test_a_game_with_fetch_lands_and_scry_keeps_its_invariants():
    from playtest.agents.heuristic import HeuristicAgent
    from playtest.engine.cards import DeckSpec
    from playtest.engine.game import Game

    fetch = compiled_spec("Fetch", "Land", "", FETCH)
    look = compiled_spec("Look", "Sorcery", "{G}", "Scry 2.\nDraw a card.")
    forest = card("Forest", "Basic Land", produces="G")
    library = [fetch] * 8 + [forest] * 22 + [look] * 10 + [card(f"Bear {i}", "Creature", "{1}{G}", 2, 2) for i in range(20)]
    decks = [DeckSpec(name, None, tuple(library)) for name in "AB"]
    result = Game(decks, [HeuristicAgent(0), HeuristicAgent(1)], DUEL, seed=5, max_rounds=30, check_invariants=True).run()
    kinds = [e.event.kind for e in result.events]
    assert result.winner_seat is not None
    assert any(e.event.payload.get("effect") == "search" for e in result.events)
    assert any(e.event.payload.get("effect") == "scry" for e in result.events)
    assert "agent_fallback" not in kinds


# -- hand disruption, digging, counter-unless, back-references, artifact tokens -------


def _cast_with(state, spec, choose, *, seat=0, targets=None):
    """Like :func:`_cast`, with ``choose`` answering the choices the effect asks for."""
    item = _hand(state, spec, seat=seat)
    options = [a for a in legal_actions(state, seat) if a.card == item.id]
    if targets is not None:
        options = [a for a in options if [ref for _, ref in a.targets] == targets]
    assert options, f"{spec.name} cannot be cast with {targets}"
    events = apply(state, seat, options[0])
    events += resolve_top(state, choose)
    events += check_state(state)
    return events


def _by_label(*labels):
    answers = iter(labels)

    def choose(state, seat, kind, prompt, options):
        wanted = next(answers)
        return next(o.key for o in options if o.label == wanted)

    return choose


def _library(state, seat, *specs):
    state.player(seat).library[0:0] = [Card(state.new_id(), spec, seat) for spec in specs]


THOUGHTSEIZE = (
    "Target player reveals their hand. You choose a nonland card from it. That player discards that card. You lose 2 life."
)


def test_thoughtseize_discards_the_nonland_card_its_caster_picks():
    game, state = _table()
    _mana(state, amount=1)
    _hand(state, bear(), seat=1)
    _hand(state, card("Shock", "Instant", "{1}"), seat=1)
    _hand(state, WASTES, seat=1)
    seen = []

    def choose(state_, seat, kind, prompt, options):
        seen.append((seat, kind, [o.label for o in options]))
        return next(o.key for o in options if o.label == "Discard Shock")

    _cast_with(state, compiled_spec("TS", "Sorcery", "{1}", THOUGHTSEIZE), choose, targets=["p1"])
    assert seen == [(0, "discard", ["Discard Bear 0", "Discard Shock"])]
    assert [c.name for c in state.player(1).hand] == ["Bear 0", "Wastes"]
    assert state.player(0).life == 18


def test_inquisition_offers_only_cheap_cards():
    game, state = _table()
    _mana(state, amount=1)
    _hand(state, card("Big", "Creature", "{4}{G}", 5, 5), seat=1)
    _hand(state, card("Shock", "Instant", "{1}"), seat=1)
    text = "Target player reveals their hand. You choose a nonland card from it with mana value 3 or less. That player discards that card."
    _cast(state, compiled_spec("IoK", "Sorcery", "{1}", text), targets=["p1"])
    assert [c.name for c in state.player(1).hand] == ["Big"]


def test_counter_unless_its_controller_pays():
    game, state = _table()
    spike = compiled_spec("Spike", "Instant", "{1}", "Counter target spell unless its controller pays {1}.")
    for can_pay, countered in ((True, False), (False, True)):
        game, state = _table()
        _mana(state, amount=1, seat=1)
        _mana(state, amount=2 if can_pay else 1)
        bolt = _hand(state, card("Bear", "Creature", "{1}", 2, 2))
        apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == bolt.id))
        spike_card = _hand(state, spike, seat=1)
        apply(state, 1, next(a for a in legal_actions(state, 1) if a.card == spike_card.id))
        asked = []

        def choose(state_, seat, kind, prompt, options):
            asked.append((seat, [o.label for o in options]))
            return options[0].key

        resolve_top(state, choose)
        assert any(c.name == "Bear" for c in state.player(0).graveyard) == countered
        assert asked == ([(0, ["Pay {1}", "Don't pay"])] if can_pay else [])


def test_stock_up_puts_two_in_hand_and_the_rest_on_the_bottom():
    game, state = _table()
    _mana(state, amount=3)
    names = [f"Card {i}" for i in range(5)]
    _library(state, 0, *(card(n, "Sorcery", "{1}") for n in names))
    size = len(state.player(0).library)
    text = "Look at the top five cards of your library. Put two of them into your hand and the rest on the bottom of your library in any order."
    _cast_with(state, compiled_spec("Dig", "Sorcery", "{3}", text), _by_label("Take Card 3", "Take Card 1"))
    assert sorted(c.name for c in state.player(0).hand) == ["Card 1", "Card 3"]
    assert len(state.player(0).library) == size - 2
    assert sorted(c.name for c in state.player(0).library[-3:]) == ["Card 0", "Card 2", "Card 4"]


def test_brainstorm_draws_then_puts_back_the_cards_its_player_picks():
    game, state = _table()
    _mana(state, amount=1)
    _hand(state, card("Keep", "Sorcery", "{1}"))
    _library(state, 0, *(card(f"Top {i}", "Sorcery", "{1}") for i in range(3)))
    text = "Draw three cards, then put two cards from your hand on top of your library in any order."
    _cast_with(state, compiled_spec("Storm", "Instant", "{1}", text), _by_label("Put back Top 2", "Put back Top 0"))
    assert sorted(c.name for c in state.player(0).hand) == ["Keep", "Top 1"]
    assert [c.name for c in state.player(0).library[:2]] == ["Top 0", "Top 2"]


def test_ponder_reorders_or_shuffles_then_draws():
    game, state = _table()
    _mana(state, amount=1)
    _library(state, 0, *(card(f"Top {i}", "Sorcery", "{1}") for i in range(3)))
    text = "Look at the top three cards of your library, then put them back in any order. You may shuffle.\nDraw a card."
    spec = compiled_spec("Ponder", "Sorcery", "{1}", text)
    _cast_with(state, spec, _by_label("Keep them", "Top 2 on top", "Top 0 on top"))
    assert [c.name for c in state.player(0).hand] == ["Top 2"]
    assert [c.name for c in state.player(0).library[:2]] == ["Top 0", "Top 1"]


def test_swords_gives_life_to_the_controller_equal_to_last_known_power():
    game, state = _table()
    _mana(state, amount=1)
    target = _put(state, bear(), seat=1)
    target.plus_counters = 3
    spec = compiled_spec("Swords", "Instant", "{1}", "Exile target creature. Its controller gains life equal to its power.")
    _cast(state, spec, targets=[_ref(target)])
    assert target not in state.battlefield
    assert state.player(1).life == 25 and state.player(0).life == 20


def test_a_back_reference_follows_its_target_when_it_becomes_illegal():
    game, state = _table()
    _mana(state, amount=1)
    target = _put(state, bear(), seat=1)
    spec = compiled_spec("Swords", "Instant", "{1}", "Exile target creature. Its controller gains life equal to its power.")
    item = _hand(state, spec)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == item.id))
    state.battlefield.remove(target)
    events = resolve_top(state)
    assert state.player(1).life == 20
    assert any(e.kind == EventKind.FIZZLE for e in events)


def test_spell_restrictions_and_opponent_controlled_permanents():
    game, state = _table()
    _mana(state, amount=4, seat=1)
    one = _hand(state, card("One", "Instant", "{1}"), seat=1)
    two = _hand(state, card("Two", "Instant", "{2}"), seat=1)
    state.active_seat = 1
    for spell in (one, two):
        apply(state, 1, next(a for a in legal_actions(state, 1) if a.card == spell.id))
    state.active_seat = 0
    misstep = _hand(state, compiled_spec("Misstep", "Instant", "{1}", "Counter target spell with mana value 1."))
    _mana(state, amount=1)
    refs = {dict(a.targets)["0:t1"] for a in legal_actions(state, 0) if a.card == misstep.id}
    assert refs == {f"s{i.id}" for i in state.stack if i.card.id == one.id}

    game, state = _table()
    _mana(state, amount=2)
    mine = _put(state, card("My Rock", "Artifact", "{1}"))
    theirs = _put(state, card("Their Rock", "Artifact", "{1}"), seat=1)
    spec = compiled_spec("Enchanter", "Creature", "{2}", "When this creature enters, destroy target artifact or enchantment an opponent controls.", 2, 2)
    _cast(state, spec)
    from playtest.engine.effects import target_choices

    ability = spec.program.abilities[0]
    assert target_choices(state, ability.targets[0], 0) == [_ref(theirs)]
    assert mine in state.battlefield


def test_treasure_clue_and_blood_tokens():
    game, state = _table()
    _mana(state, amount=1)
    _cast(state, compiled_spec("Strike", "Sorcery", "{1}", "Create a Treasure token."))
    treasure = next(p for p in state.battlefield if p.name == "Treasure")
    _hand(state, card("Shock", "Instant", "{1}"))
    shock = state.player(0).hand[-1]
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == shock.id))
    assert treasure not in state.battlefield

    game, state = _table()
    _mana(state, amount=3)
    _cast(state, compiled_spec("Inspector", "Sorcery", "{1}", "Investigate."))
    clue = next(p for p in state.battlefield if p.name == "Clue")
    hand = len(state.player(0).hand)
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE and a.permanent == clue.id))
    resolve_top(state)
    assert len(state.player(0).hand) == hand + 1 and clue not in state.battlefield

    game, state = _table()
    _mana(state, amount=2)
    epicure = compiled_spec(
        "Epicure", "Creature", "{1}", "When this creature enters, it deals 1 damage to each opponent. Create a Blood token.", 1, 1
    )
    perm = _put(state, epicure)
    from playtest.engine.rules import trigger_item

    assert perm.spec.program.abilities[0].trigger == "ETB"
    blood_hand = _hand(state, card("Junk", "Sorcery", "{5}"))
    from playtest.engine.state import PendingTrigger

    item = trigger_item(state, PendingTrigger(0, "Epicure", perm.id, perm.spec.program.abilities[0]))
    state.stack.append(item)
    resolve_top(state)
    assert state.player(1).life == 19
    blood = next(p for p in state.battlefield if p.name == "Blood")
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE and a.permanent == blood.id))
    resolve_top(state)
    assert blood_hand in state.player(0).graveyard and blood not in state.battlefield


def test_an_opponent_creates_treasures_for_a_countered_spell():
    game, state = _table()
    _mana(state, amount=1, seat=1)
    spell = _hand(state, card("Rock", "Artifact", "{1}"), seat=1)
    state.active_seat = 1
    apply(state, 1, next(a for a in legal_actions(state, 1) if a.card == spell.id))
    state.active_seat = 0
    _mana(state, amount=1)
    text = "Counter target noncreature spell. Its controller creates two Treasure tokens."
    _cast(state, compiled_spec("Offer", "Instant", "{1}", text))
    assert sum(1 for p in state.battlefield if p.name == "Treasure" and p.controller == 1) == 2


def test_looking_at_a_hand_reveals_it_in_the_log():
    game, state = _table()
    _hand(state, bear(), seat=1)
    events = _cast(state, compiled_spec("Probe", "Sorcery", "{0}", "Look at target player's hand.\nDraw a card."), targets=["p1"])
    assert any("Bear" in e.text for e in events if e.payload.get("effect") == "look_at_hand")


def test_a_game_with_the_new_mechanics_keeps_its_invariants():
    from playtest.agents.heuristic import HeuristicAgent
    from playtest.engine.cards import DeckSpec
    from playtest.engine.game import Game

    island = card("Island", "Basic Land", produces="U")
    specs = [
        compiled_spec("TS", "Sorcery", "{1}", THOUGHTSEIZE),
        compiled_spec("Spike", "Instant", "{1}", "Counter target spell unless its controller pays {1}."),
        compiled_spec("Storm", "Instant", "{1}", "Draw three cards, then put two cards from your hand on top of your library in any order."),
        compiled_spec("Ponder", "Sorcery", "{1}", "Look at the top three cards of your library, then put them back in any order. You may shuffle.\nDraw a card."),
        compiled_spec("Swords", "Instant", "{1}", "Exile target creature. Its controller gains life equal to its power."),
        compiled_spec("Inspector", "Creature", "{1}", "When this creature enters, investigate.", 1, 2),
        compiled_spec("Strike", "Sorcery", "{1}", "Create a Treasure token."),
    ]
    library = [island] * 24 + specs * 3 + [card(f"Bear {i}", "Creature", "{1}", 2, 2) for i in range(15)]
    decks = [DeckSpec(name, None, tuple(library)) for name in "AB"]
    effects = set()
    for seed in range(4):
        result = Game(decks, [HeuristicAgent(0), HeuristicAgent(1)], DUEL, seed=seed, max_rounds=30, check_invariants=True).run()
        assert "agent_fallback" not in [e.event.kind for e in result.events]
        effects |= {e.event.payload.get("effect") for e in result.events}
    assert {"discard_chosen", "create_token", "put_back", "pay_unless", "gain_life"} <= effects


# -- event triggers ------------------------------------------------------------------


def _queued(state):
    """The names of the sources of waiting triggers."""
    return [t.source_name for t in state.triggers]


def test_cast_triggers_match_who_cast_and_what():
    game, state = _table()
    _mana(state, amount=2)
    _mana(state, amount=2, seat=1)
    mine = _put(state, compiled_spec("Swift", "Creature — Monk", "{R}", "Prowess", "1", "2", keywords=("Prowess",)))
    theirs = _put(state, compiled_spec("Study", "Enchantment", "{2}", "Whenever an opponent casts a spell, you draw a card."), seat=1)
    assert mine.spec.supported and theirs.spec.supported
    shock = _hand(state, card("Shock", "Instant", "{1}"))
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == shock.id))
    assert _queued(state) == ["Swift", "Study"]
    state.triggers.clear()
    resolve_top(state)
    bear_card = _hand(state, card("Bear", "Creature", "{1}", 2, 2))
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.card == bear_card.id))
    # A creature spell is not a noncreature spell.
    assert _queued(state) == ["Study"]


def test_prowess_pumps_until_end_of_turn():
    game, state = _table()
    _mana(state, amount=1)
    swift = _put(state, compiled_spec("Swift", "Creature — Monk", "{R}", "Prowess", "1", "2", keywords=("Prowess",)))
    shock = _hand(state, card("Shock", "Instant", "{1}"))
    _run_priority(game, [type("A", (), {"choose": lambda self, view, options: Decision(next((o for o in options if o.card == shock.id), options[0]))})()] * 2)
    assert power(state, swift) == 2
    cleanup_step(state, 0)
    assert power(state, swift) == 1


def test_enters_triggers_for_other_creatures_and_lands():
    game, state = _table()
    soul = _put(state, compiled_spec("Guide", "Creature — Human", "{W}", "Whenever another creature you control enters, you gain 1 life.", "1", "2"))
    chocobo = _put(state, compiled_spec("Chocobo", "Creature — Bird", "{G}", "Landfall — Whenever a land you control enters, put a +1/+1 counter on this creature.", "1", "1"))
    assert not state.triggers
    _put_untouched = enter_battlefield(state, Card(state.new_id(), bear(), 0), 0)
    assert _queued(state) == ["Guide"]
    state.triggers.clear()
    enter_battlefield(state, Card(state.new_id(), bear(), 1), 1)
    assert _queued(state) == []
    enter_battlefield(state, Card(state.new_id(), WASTES, 0), 0)
    assert _queued(state) == ["Chocobo"]


def test_combat_damage_to_a_player_triggers_and_you_attack_triggers():
    from playtest.agents.heuristic import HeuristicAgent

    game, state = _table()
    ninja = _put(state, compiled_spec("Ninja", "Creature — Ninja", "{1}", "Whenever this creature deals combat damage to a player, draw a card.", "2", "1"))
    guide = _put(state, compiled_spec("Leader", "Creature — Human", "{1}", "Whenever you attack, you gain 1 life.", "0", "4"))
    hand = len(state.player(0).hand)
    game.agents = [HeuristicAgent(0), HeuristicAgent(1)]
    game._take_turn(0)
    effects = [e.event.text for e in game.events]
    assert any("Ninja's ability triggers" in t for t in effects)
    assert any("Leader's ability triggers" in t for t in effects)
    assert state.player(1).life == 18


def test_beginning_of_combat_triggers_even_without_attackers():
    from playtest.agents.heuristic import HeuristicAgent

    game, state = _table()
    _put(state, compiled_spec("Drummer", "Enchantment", "{1}", "At the beginning of combat on your turn, you gain 2 life."))
    game.agents = [HeuristicAgent(0), HeuristicAgent(1)]
    game._take_turn(0)
    assert state.player(0).life == 22


def test_leaves_gain_life_and_opponent_draw_triggers():
    game, state = _table()
    leaver = _put(state, compiled_spec("Leaver", "Creature — Spirit", "{1}", "When this creature leaves the battlefield, draw a card.", "1", "1"))
    cleric = _put(state, compiled_spec("Cleric", "Creature — Cleric", "{1}", "Whenever you gain life, each opponent loses 1 life.", "1", "1"))
    sheoldred = _put(state, compiled_spec("Sheol", "Creature — Phyrexian", "{1}", "Whenever you draw a card, you gain 2 life.\nWhenever an opponent draws a card, they lose 2 life.", "4", "5"), seat=1)
    from playtest.engine.zones import Zone, leave_battlefield

    leave_battlefield(state, leaver, Zone.EXILE)
    assert _queued(state) == ["Leaver"]
    state.triggers.clear()
    from playtest.engine.effects import draw_cards, gain_life

    gain_life(state, 0, 2, "test")
    assert _queued(state) == ["Cleric"]
    state.triggers.clear()
    draw_cards(state, 1, 1)
    assert _queued(state) == ["Sheol"]
    state.triggers.clear()
    draw_cards(state, 0, 2)
    # One trigger per card drawn; "they" is the player who drew.
    assert _queued(state) == ["Sheol", "Sheol"]
    game._flush_triggers()
    resolve_top(state)
    assert state.player(0).life == 20
    resolve_top(state)
    assert state.player(0).life == 18 and state.player(1).life == 20


def test_enters_or_attacks_is_two_triggers_and_artifacts_to_the_graveyard_trigger():
    viper = compiled_spec("Viper", "Creature — Snake", "{1}", "Whenever this creature enters or attacks, each opponent loses 1 life.", "2", "2")
    assert [a.trigger for a in viper.program.abilities] == ["ETB", "ATTACKS"]
    bomb = compiled_spec("Bomb", "Artifact", "{1}", "When this artifact is put into a graveyard from the battlefield, draw a card.")
    game, state = _table()
    perm = _put(state, bomb)
    from playtest.engine.zones import Zone, leave_battlefield

    leave_battlefield(state, perm, Zone.GRAVEYARD)
    assert _queued(state) == ["Bomb"]


def test_a_trigger_loop_ends_with_a_limit_event():
    from playtest.agents.heuristic import HeuristicAgent
    from playtest.engine.limits import Limits

    loop = compiled_spec("Loop", "Creature — Elf", "{1}", "Whenever you gain life, you gain 1 life.", "1", "1")
    game, state = _table()
    game.limits = Limits(max_triggers_per_turn=30)
    _put(state, loop)
    from playtest.engine.effects import gain_life

    gain_life(state, 0, 1, "test")
    game.agents = [HeuristicAgent(0), HeuristicAgent(1)]
    game._priority()
    assert any(e.event.payload.get("limit") == "triggers" for e in game.events)
    assert 20 < state.player(0).life <= 52


def test_that_player_only_names_the_player_of_an_event():
    you_cast = compiled_spec("Odd", "Enchantment", "{1}", "Whenever you cast a spell, that player draws a card.")
    assert not you_cast.supported
    rhystic = compiled_spec("Tithe", "Enchantment", "{1}", "Whenever an opponent casts a spell, that player loses 1 life.")
    assert rhystic.program.abilities[0].when == "OPPONENT:SPELL"


# -- alternative and additional costs, flashback, cycling --------------------------------


FOW = "You may pay 1 life and exile a blue card from your hand rather than pay this spell's mana cost.\nCounter target spell."


def _opponent_casts(state, spec, seat=1):
    _mana(state, amount=spec.cost.mana_value, seat=seat)
    spell = _hand(state, spec, seat=seat)
    state.active_seat = seat
    apply(state, seat, next(a for a in legal_actions(state, seat) if a.card == spell.id))
    state.active_seat = 0
    return spell


def test_force_of_will_pitches_a_blue_card_and_pays_life():
    game, state = _table()
    _opponent_casts(state, card("Rock", "Artifact", "{1}"))
    fow = _hand(state, compiled_spec("FoW", "Instant", "{3}{U}{U}", FOW))
    brainstorm = _hand(state, card("Storm", "Instant", "{U}"))
    _hand(state, card("Bolt", "Instant", "{R}"))
    casts = [a for a in legal_actions(state, 0) if a.card == fow.id]
    assert [a.paid_with for a in casts] == [(brainstorm.id,)]
    assert "exiling Storm" in casts[0].label and "1 life" in casts[0].label
    apply(state, 0, casts[0])
    resolve_top(state)
    assert state.player(0).life == 19
    assert [c.name for c in state.player(0).exile] == ["Storm"]
    assert any(c.name == "Rock" for c in state.player(1).graveyard)


def test_force_of_negation_only_off_turn_and_daze_returns_an_island():
    game, state = _table()
    fon_text = "If it's not your turn, you may exile a blue card from your hand rather than pay this spell's mana cost.\nCounter target noncreature spell."
    _opponent_casts(state, card("Rock", "Artifact", "{1}"))
    fon = _hand(state, compiled_spec("FoN", "Instant", "{1}{U}{U}", fon_text))
    _hand(state, card("Storm", "Instant", "{U}"))
    state.active_seat = 0
    assert not [a for a in legal_actions(state, 0) if a.card == fon.id]
    state.active_seat = 1
    assert [a for a in legal_actions(state, 0) if a.card == fon.id]

    game, state = _table()
    _opponent_casts(state, card("Rock", "Artifact", "{1}"))
    island = _put(state, card("Island", "Basic Land", produces="U"), tapped=True)
    daze = _hand(state, compiled_spec("Daze", "Instant", "{1}{U}", "You may return an Island you control to its owner's hand rather than pay this spell's mana cost.\nCounter target spell unless its controller pays {1}."))
    [cast] = [a for a in legal_actions(state, 0) if a.card == daze.id]
    apply(state, 0, cast)
    assert island not in state.battlefield and any(c.name == "Island" for c in state.player(0).hand)


def test_a_commander_makes_fierce_guardianship_free():
    game, state = _table(rules=COMMANDER)
    _opponent_casts(state, card("Rock", "Artifact", "{1}"))
    text = "If you control a commander, you may cast this spell without paying its mana cost.\nCounter target noncreature spell."
    guard = _hand(state, compiled_spec("Guard", "Instant", "{2}{U}", text))
    assert not [a for a in legal_actions(state, 0) if a.card == guard.id]
    commander = _put(state, bear())
    commander.card.is_commander = True
    assert [a for a in legal_actions(state, 0) if a.card == guard.id]


def test_an_additional_sacrifice_cost_is_paid_on_cast():
    game, state = _table()
    _mana(state, amount=2)
    rock = _put(state, card("Rock", "Artifact", "{1}"))
    text = "As an additional cost to cast this spell, sacrifice an artifact or creature.\nDraw two cards."
    dispute = _hand(state, compiled_spec("Dispute", "Instant", "{2}", text))
    casts = [a for a in legal_actions(state, 0) if a.card == dispute.id]
    assert [a.paid_with for a in casts] == [(rock.id,)]
    hand = len(state.player(0).hand)
    apply(state, 0, casts[0])
    assert rock not in state.battlefield
    resolve_top(state)
    assert len(state.player(0).hand) == hand + 1


def test_flashback_casts_from_the_graveyard_and_exiles_the_card():
    game, state = _table()
    _mana(state, amount=3)
    looting = compiled_spec("Looting", "Sorcery", "{R}", "Draw two cards, then discard two cards.\nFlashback {3}", keywords=("Flashback",))
    assert looting.flashback
    state.player(0).graveyard.append(Card(state.new_id(), looting, 0))
    [cast] = [a for a in legal_actions(state, 0) if a.via == "flashback"]
    apply(state, 0, cast)
    resolve_top(state)
    assert [c.name for c in state.player(0).exile] == ["Looting"] and not state.player(0).graveyard[-1:] == ["Looting"]


def test_cycling_and_landcycling():
    game, state = _table()
    _mana(state, amount=3)
    _library(state, 0, card("Plains", "Basic Land", produces="W"))
    mako = _hand(state, compiled_spec("Mako", "Creature — Shark", "{U}", "Cycling {2}", "1", "1", keywords=("Cycling",)))
    revealed = _hand(state, compiled_spec("Revealed", "Sorcery", "{4}{U}{U}", "Draw three cards.\nIslandcycling {1}", keywords=("Islandcycling",)))
    cycles = [a for a in legal_actions(state, 0) if a.kind == ActionKind.CYCLE]
    assert {a.card for a in cycles} == {mako.id, revealed.id}
    apply(state, 0, next(a for a in cycles if a.card == mako.id))
    resolve_top(state)
    assert mako in state.player(0).graveyard and state.player(0).hand[-1].name == "Plains"
    _library(state, 0, card("Island", "Basic Land", produces="U"))
    apply(state, 0, next(a for a in legal_actions(state, 0) if a.kind == ActionKind.CYCLE and a.card == revealed.id))
    resolve_top(state, _by_label("Find Island"))
    assert state.player(0).hand[-1].name == "Island"


def test_a_spell_that_cant_be_countered():
    game, state = _table()
    decay = compiled_spec("Decay", "Instant", "{2}", "This spell can't be countered.\nDraw a card.")
    assert decay.uncounterable
    _mana(state, amount=1)
    _opponent_casts(state, decay)
    item = state.stack[-1]
    _cast(state, compiled_spec("Negate", "Instant", "{1}", "Counter target spell."), targets=[f"s{item.id}"])
    assert item in state.stack


def test_a_game_with_alternative_costs_flashback_and_cycling_keeps_its_invariants():
    from playtest.agents.heuristic import HeuristicAgent
    from playtest.engine.cards import DeckSpec
    from playtest.engine.game import Game

    island = card("Island", "Basic Land", produces="U")
    specs = [
        compiled_spec("FoW", "Instant", "{3}{U}{U}", FOW),
        compiled_spec("Daze", "Instant", "{1}{U}", "You may return an Island you control to its owner's hand rather than pay this spell's mana cost.\nCounter target spell unless its controller pays {1}."),
        compiled_spec("Looting", "Sorcery", "{U}", "Draw two cards, then discard two cards.\nFlashback {2}{U}", keywords=("Flashback",)),
        compiled_spec("Mako", "Creature — Shark", "{6}{U}{U}", "Cycling {2}", "8", "8", keywords=("Cycling",)),
        compiled_spec("Dispute", "Instant", "{1}{U}", "As an additional cost to cast this spell, sacrifice an artifact or creature.\nDraw two cards."),
    ]
    library = [island] * 22 + specs * 4 + [card(f"Bear {i}", "Creature", "{1}{U}", 2, 2) for i in range(18)]
    decks = [DeckSpec(name, None, tuple(library)) for name in "AB"]
    labels = set()
    for seed in range(4):
        result = Game(decks, [HeuristicAgent(0), HeuristicAgent(1)], DUEL, seed=seed, max_rounds=30, check_invariants=True).run()
        assert "agent_fallback" not in [e.event.kind for e in result.events]
        labels |= {e.event.payload.get("effect") for e in result.events}
    assert {"pitch", "cycle"} <= labels


# -- counter variants and tutors ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, zone",
    [
        ("Counter target spell. If that spell is countered this way, exile it instead of putting it into its owner's graveyard.", "exile"),
        ("Counter target spell. If that spell is countered this way, put it on top of its owner's library instead of into that player's graveyard.", "library"),
        ("Counter target spell. If that spell is countered this way, put it into its owner's hand instead of into that player's graveyard.", "hand"),
    ],
)
def test_a_countered_spell_can_go_elsewhere(text, zone):
    game, state = _table()
    rock = _opponent_casts(state, card("Rock", "Artifact", "{1}"))
    _mana(state, amount=1)
    _cast(state, compiled_spec("Lapse", "Instant", "{1}", text), targets=[f"s{state.stack[-1].id}"])
    player = state.player(1)
    where = {"exile": player.exile, "library": player.library[:1], "hand": player.hand}[zone]
    assert rock in where and rock not in player.graveyard


@pytest.mark.parametrize(
    "text, kept",
    [
        ("Search your library for an artifact or enchantment card, reveal it, then shuffle and put that card on top.", "top"),
        ("Search your library for a card, then shuffle and put that card on top. You lose 2 life.", "top"),
        ("Search your library for a creature card with toughness 2 or less, reveal it, put it into your hand, then shuffle.", "hand"),
        ("Search your library for an Equipment card, reveal it, put it into your hand, then shuffle.", "hand"),
    ],
)
def test_tutors_put_the_card_where_they_say(text, kept):
    game, state = _table()
    _mana(state, amount=2)
    wanted = {
        "artifact": card("Board", "Artifact — Equipment", "{1}"),
        "small": card("Small", "Creature", "{1}", 1, 2),
        "big": card("Big", "Creature", "{1}", 5, 5),
    }
    _library(state, 0, *wanted.values())
    asked = []

    def choose(state_, seat, kind, prompt, options):
        asked.append([o.label for o in options])
        return options[0].key

    _cast_with(state, compiled_spec("Tutor", "Sorcery", "{2}", text), choose)
    found = state.player(0).library[0] if kept == "top" else state.player(0).hand[-1]
    assert asked and f"Find {found.name}" == asked[0][0]
    if "toughness" in text:
        assert "Find Big" not in asked[0] and "Find Small" in asked[0]
    if "Equipment" in text:
        assert asked[0][:1] == ["Find Board"] and "Find Small" not in asked[0]


def test_chord_finds_a_creature_with_mana_value_up_to_x():
    game, state = _table()
    _mana(state, amount=4)
    _library(state, 0, card("Two", "Creature", "{2}", 2, 2), card("Five", "Creature", "{5}", 5, 5))
    text = "Search your library for a creature card with mana value X or less, put it onto the battlefield, then shuffle."
    spec = compiled_spec("Chord", "Sorcery", "{X}{2}", text)
    item = _hand(state, spec)
    action = next(a for a in legal_actions(state, 0) if a.card == item.id and a.x == 2)
    asked = []
    apply(state, 0, action)
    resolve_top(state, lambda st, seat, kind, prompt, options: asked.append([o.label for o in options]) or options[0].key)
    assert "Find Two" in asked[0] and "Find Five" not in asked[0]


# -- optional effects ------------------------------------------------------------------


SPELLBOMB = "When this artifact is put into a graveyard from the battlefield, you may pay {1}. If you do, draw a card."


@pytest.mark.parametrize("answer, drawn", [("c00", 1), ("c01", 0)])
def test_you_may_pay_and_if_you_do(answer, drawn):
    game, state = _table()
    _mana(state, amount=1)
    bomb = _put(state, compiled_spec("Bomb", "Artifact", "{1}", SPELLBOMB))
    from playtest.engine.zones import Zone, leave_battlefield

    leave_battlefield(state, bomb, Zone.GRAVEYARD)
    game._flush_triggers()
    hand = len(state.player(0).hand)
    asked = []
    resolve_top(state, lambda st, seat, kind, prompt, options: asked.append((kind, [o.label for o in options])) or answer)
    assert asked == [("may", ["Pay {1}", "Don't"])]
    assert len(state.player(0).hand) == hand + drawn


def test_if_you_do_is_skipped_when_the_payment_is_impossible():
    game, state = _table()
    bomb = _put(state, compiled_spec("Bomb", "Artifact", "{1}", SPELLBOMB))
    from playtest.engine.zones import Zone, leave_battlefield

    leave_battlefield(state, bomb, Zone.GRAVEYARD)
    game._flush_triggers()
    hand = len(state.player(0).hand)
    resolve_top(state)
    assert len(state.player(0).hand) == hand


def test_you_may_search_on_enters():
    game, state = _table()
    _library(state, 0, card("Small", "Creature", "{1}", 1, 2))
    text = "When this creature enters, you may search your library for a creature card with toughness 2 or less, reveal it, put it into your hand, then shuffle."
    recruiter = compiled_spec("Recruiter", "Creature — Human", "{3}", text, "1", "1")
    assert recruiter.program.abilities[0].ops[0].optional
    _put(state, recruiter)
    enter_battlefield(state, Card(state.new_id(), recruiter, 0), 0)
    game._flush_triggers()
    resolve_top(state, lambda st, seat, kind, prompt, options: "c00" if kind == "may" else next(o.key for o in options if o.label == "Find Small"))
    assert state.player(0).hand[-1].name == "Small"


def test_if_you_do_needs_an_earlier_optional_op():
    from playtest.engine import dsl

    facts = __import__("playtest.engine.cardrules", fromlist=["characteristics"]).characteristics(
        __import__("tests.cards", fromlist=["raw_card"]).raw_card("X", "Sorcery", "{1}", "Draw a card. If you do, draw a card.")
    )
    payload = {
        "version": dsl.DSL_VERSION,
        "abilities": [
            {
                "trigger": "SPELL",
                "targets": {"t1": {"selector": "YOU"}},
                "ops": [{"op": "DRAW_CARDS", "target": "t1", "amount": 1}, {"op": "DRAW_CARDS", "target": "t1", "amount": 1, "if_done": True}],
            }
        ],
    }
    with pytest.raises(dsl.DslError, match="if_done"):
        dsl.validate_program(payload, facts)


def test_a_sacrifice_cost_lets_the_player_pick_the_permanent():
    game, state = _table()
    outlet = _put(state, compiled_spec("Bombardment", "Enchantment", "{2}", "Sacrifice a creature: This enchantment deals 1 damage to any target."))
    token = _put(state, card("Goblin", "Creature", "", 1, 1))
    big = _put(state, card("Big", "Creature", "{5}", 5, 5))
    actions = [a for a in legal_actions(state, 0) if a.kind == ActionKind.ACTIVATE and dict(a.targets).get("0:t1") == "p1"]
    assert sorted(a.paid_with for a in actions) == sorted([(token.id,), (big.id,)])
    apply(state, 0, next(a for a in actions if a.paid_with == (token.id,)))
    resolve_top(state)
    assert token not in state.battlefield and big in state.battlefield and state.player(1).life == 19
    other = compiled_spec("Shaman", "Creature — Goblin", "{1}", "Sacrifice another creature: You gain 1 life.", "1", "1")
    shaman = _put(state, other)
    assert all(a.paid_with != (shaman.id,) for a in legal_actions(state, 0) if a.permanent == shaman.id)
    assert outlet in state.battlefield
