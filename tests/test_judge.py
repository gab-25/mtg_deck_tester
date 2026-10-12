"""Tests for the card judge, the compiled-rules cache and the coverage report (no network)."""

import io

import pytest
from django.core.management import call_command

from decks.cache import DbCardCache, FileCardCache
from decks.models import CardRule
from playtest import judge
from playtest.agents import typesafe
from playtest.engine import dsl
from playtest.engine.coverage import CoverageEntry, ReasonKind, Status, coverage_report

from .cards import FakeJev, answer, basic_land, raw_card, stub_jev_http

BOLT = raw_card("Lightning Bolt", "Instant", "{R}", "Lightning Bolt deals 3 damage to any target.")


@pytest.fixture
def cache(tmp_path):
    return FileCardCache(str(tmp_path))


def test_vanilla_creature_and_basic_land_need_no_request(cache):
    jev = FakeJev()
    for card in (raw_card("Grizzly Bears"), basic_land()):
        compiled = judge.compile_card(card, cache, ask=jev)
        assert compiled.status == Status.SUPPORTED
        assert compiled.program == {"version": dsl.DSL_VERSION, "abilities": []}
    assert jev.requests == []


def test_a_card_needing_the_judge_makes_exactly_one_request(cache):
    jev = FakeJev()
    compiled = judge.compile_card(BOLT, cache, ask=jev)
    assert compiled.status == Status.SUPPORTED and compiled.asked
    assert compiled.model == "typesafe/jev-1.13"
    [(state, questions)] = jev.requests
    # The state is the card and its residual text, nothing else.
    assert state == "Lightning Bolt\nInstant\n\nthis spell deals 3 damage to any target."
    assert set(questions) == {"op_0", "op_0_rev", "target_0", "target_0_rev"}
    # The reverse question really reverses the options.
    assert list(questions["op_0_rev"]["criteria"]) == list(reversed(questions["op_0"]["criteria"]))
    [ability] = compiled.card_program().abilities
    assert ability.ops == (dsl.Op("DEAL_DAMAGE", target="t1", amount=3),)


SAMPLES = {
    "DEAL_DAMAGE": ("Instant", "This spell deals 2 damage to target creature."),
    "GAIN_LIFE": ("Instant", "You gain 4 life."),
    "LOSE_LIFE": ("Sorcery", "Each opponent loses 2 life."),
    "DRAW_CARDS": ("Sorcery", "Draw two cards."),
    "DISCARD_CARDS": ("Sorcery", "Target player discards two cards."),
    "DESTROY": ("Instant", "Destroy target creature."),
    "EXILE": ("Instant", "Exile target nonland permanent."),
    "RETURN_TO_HAND": ("Instant", "Return target creature to its owner's hand."),
    "TAP": ("Instant", "Tap target creature."),
    "UNTAP": ("Instant", "Untap target land."),
    "ADD_MANA": ("Instant", "Add {R}{R}{R}."),
    "CREATE_TOKEN": ("Sorcery", "Create two 1/1 white Soldier creature tokens."),
    "MODIFY_PT": ("Instant", "Target creature gets +3/+3 until end of turn."),
    "GRANT_KEYWORD": ("Instant", "Target creature gains flying until end of turn."),
    "PUT_COUNTERS": ("Sorcery", "Put two +1/+1 counters on target creature."),
    "SACRIFICE": ("Sorcery", "Each opponent sacrifices a creature."),
    "MILL": ("Sorcery", "Target player mills three cards."),
    "COUNTER_SPELL": ("Instant", "Counter target spell."),
    "SEARCH_LIBRARY": ("Sorcery", "Search your library for a basic land card, put it onto the battlefield tapped, then shuffle."),
    "SCRY": ("Sorcery", "Scry 2."),
    "SURVEIL": ("Sorcery", "Surveil 2."),
    "DISCARD_CHOSEN": (
        "Sorcery",
        "Target opponent reveals their hand. You choose a noncreature, nonland card from it. That player discards that card.",
    ),
    "LOOK_AT_HAND": ("Sorcery", "Look at target player's hand."),
    "DIG": ("Sorcery", "Look at the top four cards of your library. Put one of them into your hand and the rest into your graveyard."),
    "PUT_BACK": ("Instant", "Put a card from your hand on top of your library."),
    "ARRANGE_TOP": ("Sorcery", "Look at the top three cards of your library, then put them back in any order."),
    "COUNTER_UNLESS_PAYS": ("Instant", "Counter target spell unless its controller pays {3}."),
    "CREATE_ARTIFACT_TOKEN": ("Sorcery", "Create two Treasure tokens."),
    "PAY_MANA": ("Sorcery", "You may pay {2}. If you do, draw a card."),
}


def test_vocabulary_criteria_and_validator_cover_exactly_the_same_ops(cache):
    assert set(SAMPLES) == set(dsl.OPS)
    jev = FakeJev()
    for op, (type_line, text) in SAMPLES.items():
        compiled = judge.compile_card(raw_card(f"Card {op}", type_line, "{1}", text), cache, ask=jev)
        assert compiled.status == Status.SUPPORTED, (op, compiled.reason)
        assert compiled.card_program().abilities[0].ops[0].op == op
    criteria = jev.requests[0][1]["op_0"]["criteria"]
    assert set(criteria) == set(dsl.OPS) | {dsl.NONE_OPTION}


@pytest.mark.parametrize(
    "overrides, kind, text",
    [
        ({"op_0": answer("NONE"), "op_0_rev": answer("NONE")}, ReasonKind.NONE, "clause 1"),
        ({"op_0_rev": answer("DESTROY")}, ReasonKind.UNSTABLE, "clause 1"),
        ({"op_0": answer("DEAL_DAMAGE", 0.6)}, ReasonKind.LOW_CONFIDENCE, "clause 1"),
        ({"op_0": answer("DRAW_CARDS"), "op_0_rev": answer("DRAW_CARDS")}, ReasonKind.INVALID, "clause 1"),
        ({"target_0": answer("YOU"), "target_0_rev": answer("YOU")}, ReasonKind.INVALID, "clause 1"),
        ({"op_0": answer("BURN")}, ReasonKind.INVALID, "clause 1"),
    ],
)
def test_doubtful_answers_make_the_card_unsupported(cache, overrides, kind, text):
    compiled = judge.compile_card(BOLT, cache, ask=FakeJev(overrides))
    assert compiled.status == Status.UNSUPPORTED
    assert compiled.reason_kind == kind
    assert text in compiled.reason


def test_unreachable_judge_yields_unsupported_and_is_not_cached(cache, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    compiled = judge.compile_card(BOLT, cache)
    assert compiled.status == Status.UNSUPPORTED
    assert compiled.reason_kind == ReasonKind.NOT_ASKED
    assert cache.get_rule("oracle-Lightning Bolt", dsl.DSL_VERSION) is None


def test_a_network_failure_yields_unsupported(cache, monkeypatch):
    import requests

    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "post", offline)
    compiled = judge.compile_card(BOLT, cache, ask=lambda s, q: typesafe.system_one(s, q, key="k"))
    assert compiled.reason_kind == ReasonKind.NOT_ASKED
    assert "offline" in compiled.reason


@pytest.mark.django_db
def test_the_second_compilation_makes_no_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    calls = stub_jev_http(monkeypatch)
    cache = DbCardCache()
    first = judge.compile_card(BOLT, cache)
    second = judge.compile_card(BOLT, cache)
    assert len(calls) == 1
    assert first.program == second.program and not second.asked
    rule = CardRule.objects.get()
    assert (rule.oracle_id, rule.dsl_version, rule.model) == ("oracle-Lightning Bolt", dsl.DSL_VERSION, "typesafe/jev-1.13")


@pytest.mark.django_db
def test_the_cache_key_includes_the_dsl_version(monkeypatch):
    jev = FakeJev()
    cache = DbCardCache()
    judge.compile_card(BOLT, cache, ask=jev)
    newer = str(int(dsl.DSL_VERSION) + 1)
    monkeypatch.setattr(dsl, "DSL_VERSION", newer)
    with pytest.raises(dsl.DslError):
        # A program cached for v1 is not a v2 program: it is recompiled, not reused.
        dsl.validate_program(CardRule.objects.get().program, judge.characteristics(BOLT))
    assert DbCardCache().get_rule("oracle-Lightning Bolt", newer) is None


@pytest.mark.django_db
def test_refusals_from_jev_are_cached_too():
    cache = DbCardCache()
    jev = FakeJev({"op_0": answer("NONE"), "op_0_rev": answer("NONE")})
    judge.compile_card(BOLT, cache, ask=jev)
    again = judge.compile_card(BOLT, cache, ask=jev)
    assert len(jev.requests) == 1
    assert again.reason_kind == ReasonKind.NONE


def test_compile_deck_falls_back_to_the_stored_card(cache):
    stored = [{"quantity": 3, "is_commander": False, "data": {"name": "Nowhere Bear", "type_line": "Creature — Bear",
               "faces": [{"type_line": "Creature — Bear", "mana_cost": "{1}{G}", "rules_text": ""}], "power": "2", "toughness": "2"}}]
    [entry] = judge.compile_deck(stored, cache, ask=FakeJev(), fetch=lambda name, c: None)
    assert entry.quantity == 3 and entry.card.status == Status.SUPPORTED
    assert entry.card.facts.power == 2


def test_compile_deck_compiles_each_distinct_card_once(cache):
    jev = FakeJev()
    raws = {"Lightning Bolt": BOLT, "Forest": basic_land()}
    cards = [
        {"quantity": 1, "is_commander": False, "data": {"name": "Lightning Bolt"}},
        {"quantity": 30, "is_commander": False, "data": {"name": "Forest"}},
        {"quantity": 1, "is_commander": False, "data": {"name": "Lightning Bolt"}},
    ]
    entries = judge.compile_deck(cards, cache, ask=jev, fetch=lambda name, c: raws[name])
    assert len(entries) == 3 and len(jev.requests) == 1


# -- coverage ----------------------------------------------------------------------


def test_coverage_is_weighted_by_quantity_and_flags_the_warnings():
    report = coverage_report(
        [
            CoverageEntry("Commander", 1, True, Status.UNSUPPORTED, ReasonKind.NONE, "clause 1: …"),
            CoverageEntry("Forest", 37, False, Status.SUPPORTED),
            CoverageEntry("Bolt", 1, False, Status.UNSUPPORTED, ReasonKind.BLOCKLISTED, "storm", spell=True),
            CoverageEntry("Dfc", 1, False, Status.PARTIAL),
        ]
    )
    assert (report["total"], report["supported"], report["partial"], report["unsupported"]) == (40, 37, 1, 2)
    assert report["percent"] == 95
    assert report["commander_unsupported"] and report["dead_spells"] == 1
    assert [r["kind"] for r in report["reasons"]] == [ReasonKind.BLOCKLISTED, ReasonKind.NONE]
    assert report["unsupported_cards"][0]["name"] == "Commander"


@pytest.mark.django_db
def test_card_coverage_command_names_unsupported_cards(tmp_path, monkeypatch):
    raws = {
        "Lightning Bolt": BOLT,
        "Forest": basic_land(),
        "Tutor": raw_card("Tutor", "Sorcery", "{1}{B}", "Copy target instant or sorcery spell."),
        "Bear Lord": raw_card("Bear Lord", "Legendary Creature — Bear"),
    }
    monkeypatch.setattr(judge, "fetch_card_raw", lambda name, cache: raws[name])
    monkeypatch.setattr(judge.typesafe, "system_one", FakeJev())
    decklist = tmp_path / "deck.txt"
    decklist.write_text("Commander\n1 Bear Lord\n\nDeck\n1 Lightning Bolt\n1 Tutor\n97 Forest\n")
    out = io.StringIO()
    call_command("card_coverage", str(decklist), stdout=out)
    text = out.getvalue()
    assert "99% of 100 cards" in text
    assert "1 dead spell" in text
    assert "1x Tutor [spell] — copies" in text


@pytest.mark.parametrize(
    "type_line, text, reason",
    [
        # "it" after a target is that target, not this creature.
        ("Creature — Bear", "When this creature enters, tap target creature. It deals 1 damage to you.", "ambiguous"),
        # "its controller" with nothing targeted before.
        ("Sorcery", "Its controller gains 2 life.", "refers to nothing"),
    ],
)
def test_back_references_need_an_earlier_target(cache, type_line, text, reason):
    compiled = judge.compile_card(raw_card("Ref", type_line, "{1}", text), cache, ask=FakeJev())
    assert compiled.status == Status.UNSUPPORTED
    assert reason in compiled.reason


def test_a_back_reference_names_the_target_it_follows(cache):
    text = "Exile target creature. Its controller gains life equal to its power."
    compiled = judge.compile_card(raw_card("Swords", "Instant", "{W}", text), cache, ask=FakeJev())
    [ability] = compiled.card_program().abilities
    assert ability.target("t2") == dsl.Target("t2", "CONTROLLER_OF_IT", of="t1")
    assert ability.ops[1] == dsl.Op("GAIN_LIFE", target="t2", amount="POWER_OF_IT", of="t1")
