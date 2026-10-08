"""Tests for the judge (Jev stubbed: no network)."""

import pytest

from decks.cache import DbCardCache, FileCardCache
from playtest import judge
from playtest.agents import typesafe
from playtest.engine.dsl import DSL_VERSION


def raw(name, type_line, oracle_text="", mana_cost="", **fields):
    return {
        "oracle_id": f"oracle-{name}",
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "mana_cost": mana_cost,
        "cmc": 1.0,
        **fields,
    }


BOLT = raw("Lightning Bolt", "Instant", "Lightning Bolt deals 3 damage to any target.", "{R}")


def answer(choice, confidence=0.95):
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": {choice: confidence},
        "confidence": confidence,
    }


class FakeJev:
    """Answers each question from a table keyed by question kind (op / target / amount)."""

    def __init__(
        self,
        op="DEAL_DAMAGE",
        rev=None,
        target="ANY_TARGET",
        target_rev=None,
        amount=None,
        confidence=0.95,
    ):
        self.table = {
            "op": op,
            "rev": rev or op,
            "target": target,
            "target_rev": target_rev or target,
            "amount": amount,
        }
        self.confidence = confidence
        self.calls = []

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        answers = {}
        for key in questions:
            kind = key.split("_")[0]
            if key.endswith("_rev"):
                kind = "rev" if kind == "op" else "target_rev"
            answers[key] = answer(self.table[kind], self.confidence)
        return {"model": "typesafe/jev-1.13", "answers": answers}


@pytest.fixture
def cache(tmp_path):
    return FileCardCache(str(tmp_path))


def test_a_vanilla_creature_and_a_basic_land_need_no_jev_call(cache):
    jev = FakeJev()
    bear = judge.compile_card(
        raw("Grizzly Bears", "Creature — Bear", power="2", toughness="2"), cache, ask=jev
    )
    forest = judge.compile_card(
        raw("Forest", "Basic Land — Forest", "({T}: Add {G}.)", produced_mana=["G"]), cache, ask=jev
    )
    assert bear.status == forest.status == "supported"
    assert bear.program == {"abilities": []}
    assert jev.calls == []


def test_a_burn_spell_compiles_with_one_request(cache):
    jev = FakeJev()
    card = judge.compile_card(BOLT, cache, ask=jev)
    assert card.status == "supported"
    assert card.model == "typesafe/jev-1.13"
    assert card.program == {
        "abilities": [
            {
                "trigger": "SPELL",
                "cost": None,
                "mode_group": None,
                "targets": [{"id": "t1", "selector": "ANY_TARGET", "restriction": None}],
                "ops": [{"op": "DEAL_DAMAGE", "target": "t1", "amount": 3}],
            }
        ]
    }
    [(state, questions)] = jev.calls
    assert state == {
        "card": "Lightning Bolt",
        "type_line": "Instant",
        "rules_text": BOLT["oracle_text"],
    }
    assert set(questions) == {"op_0_0", "op_0_0_rev", "target_0_0", "target_0_0_rev"}


def test_the_reverse_question_lists_the_options_in_reverse(cache):
    jev = FakeJev()
    judge.compile_card(BOLT, cache, ask=jev)
    [(_, questions)] = jev.calls
    forward = list(questions["op_0_0"]["criteria"])
    assert list(questions["op_0_0_rev"]["criteria"]) == forward[::-1]
    assert forward[-1] == judge.NONE


def test_the_second_compilation_makes_no_request(cache):
    jev = FakeJev()
    first = judge.compile_card(BOLT, cache, ask=jev)
    second = judge.compile_card(BOLT, cache, ask=jev)
    assert len(jev.calls) == 1
    assert second.program == first.program
    assert cache.get_rule("oracle-Lightning Bolt", DSL_VERSION)["status"] == "supported"


@pytest.mark.parametrize(
    "jev, kind",
    [
        (FakeJev(op="NONE"), "none"),
        (FakeJev(op="DEAL_DAMAGE", rev="LOSE_LIFE"), "unstable"),
        (FakeJev(confidence=0.5), "low_confidence"),
        (FakeJev(target="YOU", op="DESTROY"), "invalid"),
        (FakeJev(target="NONE"), "none"),
        (FakeJev(target="ANY_TARGET", target_rev="TARGET_CREATURE"), "unstable"),
    ],
)
def test_doubtful_answers_make_the_card_unsupported_with_a_reason(cache, jev, kind):
    card = judge.compile_card(BOLT, cache, ask=jev)
    assert card.status == "unsupported"
    assert card.program is None
    assert card.reason_kind == kind
    assert "clause 1" in card.reason or kind == "invalid"


def test_a_blocklisted_card_is_unsupported_without_a_call(cache):
    jev = FakeJev()
    card = judge.compile_card(
        raw("Wilds", "Land", "{T}, Sacrifice Wilds: Search your library for a land card."),
        cache,
        ask=jev,
    )
    assert (card.status, card.reason_kind) == ("unsupported", "blocklisted")
    assert jev.calls == []


def test_unreadable_text_is_unsupported_without_a_call(cache):
    jev = FakeJev()
    card = judge.compile_card(raw("Maybe", "Sorcery", "You may draw a card."), cache, ask=jev)
    assert (card.status, card.reason_kind) == ("unsupported", "unreadable")
    assert jev.calls == []


@pytest.mark.parametrize(
    "error", ["no OpenRouter API key configured", "Jev request failed: offline"]
)
def test_jev_unreachable_is_unsupported_and_not_cached(cache, error):
    def down(state, questions):
        raise typesafe.TypeSafeError(error)

    card = judge.compile_card(BOLT, cache, ask=down)
    assert (card.status, card.reason_kind, card.reason) == ("unsupported", "not_asked", error)
    assert cache.get_rule("oracle-Lightning Bolt", DSL_VERSION) is None


@pytest.mark.parametrize("jev", [FakeJev(op="FIREBALL"), FakeJev(amount=None, confidence=None)])
def test_a_malformed_answer_is_unsupported_and_not_cached(cache, jev):
    card = judge.compile_card(BOLT, cache, ask=jev)
    assert (card.status, card.reason_kind) == ("unsupported", "not_asked")
    assert "malformed" in card.reason
    assert cache.get_rule("oracle-Lightning Bolt", DSL_VERSION) is None


def test_a_missing_key_never_raises(cache, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    card = judge.compile_card(BOLT, cache)
    assert card.reason_kind == "not_asked"


def test_several_numbers_in_one_clause_are_refused_without_a_call(cache):
    jev = FakeJev()
    card = judge.compile_card(
        raw("Twin Bolt", "Instant", "Twin Bolt deals 2 damage to any target 3 times."),
        cache,
        ask=jev,
    )
    assert (card.status, card.reason_kind) == ("unsupported", "unreadable")
    assert jev.calls == []


def test_a_single_amount_is_read_in_code(cache):
    jev = FakeJev()
    judge.compile_card(BOLT, cache, ask=jev)
    [(_, questions)] = jev.calls
    assert "amount_0_0" not in questions


def test_a_player_operation_without_a_subject_is_the_controllers(cache):
    jev = FakeJev(op="DRAW_CARDS")
    card = judge.compile_card(
        raw("Divination", "Sorcery", "Draw two cards.", "{2}{U}"), cache, ask=jev
    )
    assert card.status == "supported"
    ability = card.program["abilities"][0]
    assert ability["targets"] == [{"id": "t1", "selector": "YOU", "restriction": None}]
    assert ability["ops"] == [{"op": "DRAW_CARDS", "target": "t1", "amount": 2}]


def test_a_continuation_reuses_the_previous_target(cache):
    class Jev(FakeJev):
        def __call__(self, state, questions):
            reply = super().__call__(state, questions)
            reply["answers"]["op_0_1"] = reply["answers"]["op_0_1_rev"] = answer("LOSE_LIFE")
            return reply

    jev = Jev(op="DRAW_CARDS", target="TARGET_PLAYER")
    card = judge.compile_card(
        raw("Sign", "Sorcery", "Target player draws two cards and loses 2 life."), cache, ask=jev
    )
    assert card.status == "supported", card.reason
    ability = card.program["abilities"][0]
    assert ability["targets"] == [{"id": "t1", "selector": "TARGET_PLAYER", "restriction": None}]
    assert [op["target"] for op in ability["ops"]] == ["t1", "t1"]


def test_a_double_faced_card_is_partial(cache):
    card = {
        "oracle_id": "o-mdfc",
        "name": "Bear // Cave",
        "type_line": "Creature — Bear // Land",
        "cmc": 2.0,
        "card_faces": [
            {
                "name": "Bear",
                "type_line": "Creature — Bear",
                "mana_cost": "{1}{G}",
                "oracle_text": "",
                "power": "2",
                "toughness": "2",
            },
            {"name": "Cave", "type_line": "Land", "mana_cost": "", "oracle_text": "{T}: Add {G}."},
        ],
    }
    assert judge.compile_card(card, cache, ask=FakeJev()).status == "partial"


@pytest.mark.django_db
def test_compile_deck_reports_unknown_cards_and_compiles_each_name_once():
    cache = DbCardCache()
    cache.set_card("card_en_lightning_bolt", BOLT)
    jev = FakeJev()
    fetched = []

    def fetch(name, cache):
        fetched.append(name)
        return cache.get_card("card_en_" + name.lower().replace(" ", "_"))

    cards = judge.compile_deck(
        [
            {"name": "Lightning Bolt", "quantity": 1, "is_commander": False},
            {"name": "Lightning Bolt", "quantity": 2, "is_commander": False},
            {"name": "Nonexistent", "quantity": 1, "is_commander": False},
        ],
        cache,
        ask=jev,
        fetch=fetch,
    )
    assert [c.quantity for c in cards] == [1, 2, 1]
    assert cards[0].card.status == "supported"
    assert (cards[2].card.status, cards[2].card.reason_kind) == ("unsupported", "not_found")
    assert fetched == ["Lightning Bolt", "Nonexistent"]
    assert len(jev.calls) == 1


def test_the_target_question_is_asked_in_both_orders_with_a_none_option(cache):
    jev = FakeJev()
    judge.compile_card(BOLT, cache, ask=jev)
    [(_, questions)] = jev.calls
    forward = list(questions["target_0_0"]["criteria"])
    assert judge.NONE in forward
    assert list(questions["target_0_0_rev"]["criteria"]) == forward[::-1]


def test_an_answer_outside_the_options_is_malformed_and_not_cached(cache):
    card = judge.compile_card(BOLT, cache, ask=FakeJev(target="BOGUS"))
    assert (card.status, card.reason_kind) == ("unsupported", "not_asked")
    assert cache.get_rule("oracle-Lightning Bolt", DSL_VERSION) is None


def test_cards_without_an_oracle_id_never_share_a_cache_entry(cache):
    jev = FakeJev(op="DRAW_CARDS")
    first = raw("First", "Sorcery", "Draw two cards.")
    second = raw("Second", "Sorcery", "Draw three cards.")
    first["oracle_id"] = second["oracle_id"] = None
    judge.compile_card(first, cache, ask=jev)
    card = judge.compile_card(second, cache, ask=jev)
    assert len(jev.calls) == 2
    assert card.program["abilities"][0]["ops"][0]["amount"] == 3


def test_only_jev_answers_are_cached(cache):
    judge.compile_card(
        raw("Wilds", "Land", "Search your library for a land card."), cache, ask=FakeJev()
    )
    judge.compile_card(raw("Maybe", "Sorcery", "You may draw a card."), cache, ask=FakeJev())
    judge.compile_card(raw("Grizzly Bears", "Creature — Bear", power="2", toughness="2"), cache)
    for name in ("Wilds", "Maybe", "Grizzly Bears"):
        assert cache.get_rule(f"oracle-{name}", DSL_VERSION) is None
