"""Raw Scryfall cards and a stand-in Jev, for the compiler tests (no network)."""

import requests

from playtest.engine import dsl


def raw_card(
    name,
    type_line="Creature — Bear",
    mana_cost="{1}{G}",
    oracle_text="",
    power=None,
    toughness=None,
    keywords=(),
    produced_mana=(),
    colors=None,
    **extra,
):
    """A raw Scryfall card JSON, as ``fetch_card_raw`` returns it."""
    card = {
        "object": "card",
        "id": f"id-{name}",
        "oracle_id": f"oracle-{name}",
        "name": name,
        "lang": "en",
        "type_line": type_line,
        "mana_cost": mana_cost,
        "cmc": float(sum(int(c) if c.isdigit() else 1 for c in mana_cost.replace("{", " ").replace("}", " ").split())),
        "oracle_text": oracle_text,
        "keywords": list(keywords),
        "colors": list(colors if colors is not None else [c for c in "WUBRG" if c in mana_cost]),
        "color_identity": [],
        "produced_mana": list(produced_mana),
        "legalities": {"commander": "legal", "duel": "legal"},
    }
    if "Creature" in type_line:
        card["power"] = power if power is not None else "2"
        card["toughness"] = toughness if toughness is not None else "2"
    card.update(extra)
    return card


def basic_land(name="Forest", color="G"):
    return raw_card(
        name,
        f"Basic Land — {name}",
        "",
        f"({{T}}: Add {{{color}}}.)",
        produced_mana=[color],
        colors=[],
    )


def _answer(pick, confidence=1.0):
    return {"type": "choice", "choice": pick, "confidence": confidence, "probabilities": {pick: confidence}}


def grammar_answers(questions, *, overrides=None):
    """What a perfect judge would answer: the op and selector the grammar reads."""
    overrides = overrides or {}
    answers = {}
    for key, question in questions.items():
        if key in overrides:
            answers[key] = overrides[key]
            continue
        clause = question["instructions"].split('"')[1]
        if key.startswith("op_"):
            pick = next((op for op in dsl.OPS if dsl.parse_clause(op, clause)), dsl.NONE_OPTION)
        else:
            pick = dsl.NONE_OPTION
            for op in dsl.OPS:
                fields = dsl.parse_clause(op, clause)
                if fields and fields.get("selector"):
                    pick = fields["selector"]
                    break
        answers[key] = _answer(pick)
    return answers


class FakeJev:
    """A stand-in ``ask``: answers like a perfect judge and counts requests."""

    def __init__(self, overrides=None, model="typesafe/jev-1.13"):
        self.overrides = overrides or {}
        self.model = model
        self.requests = []

    def __call__(self, state, questions):
        self.requests.append((state, questions))
        return {"model": self.model, "answers": grammar_answers(questions, overrides=self.overrides), "usage": {}}


class Response:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub_jev_http(monkeypatch, overrides=None):
    """Answers OpenRouter's System One endpoint at ``requests.post`` like :class:`FakeJev`."""
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        answers = grammar_answers(json["questions"], overrides=overrides)
        return Response(payload={"model": "typesafe/jev-1.13", "answers": answers})

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def answer(pick, confidence=1.0):
    return _answer(pick, confidence)


class _NoRules:
    def get_rule(self, *args):
        return None

    def set_rule(self, *args, **kwargs):
        pass


def compiled_spec(name, type_line="Instant", mana_cost="{1}", oracle_text="", power=None, toughness=None, keywords=()):
    """A kernel card compiled from oracle text by the judge (with a perfect stand-in Jev)."""
    from playtest import judge
    from playtest.engine.cards import CardSpec

    raw = raw_card(name, type_line, mana_cost, oracle_text, power, toughness, keywords=keywords)
    compiled = judge.compile_card(raw, _NoRules(), ask=FakeJev())
    assert compiled.facts is not None
    return CardSpec.from_facts(compiled.facts, compiled.card_program() if compiled.supported else None)
