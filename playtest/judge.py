"""The judge: compiles a card's residual oracle text into an effect program, once.

Code does the structure (:mod:`playtest.engine.clauses`); Jev only picks among
closed options for each clause — which operation, which target, which amount —
in one request per card. A card it can't answer confidently and consistently
is UNSUPPORTED: a refusal is a feature, an approximation is a bug.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from decks.scryfall import fetch_card_raw

from .agents import typesafe
from .engine.cardrules import CardFacts, Residue, characteristics
from .engine.clauses import AbilityText, Clause, Unreadable, split_abilities
from .engine.dsl import DSL_VERSION, OPS, SELECTORS, DslError, validate_program

logger = logging.getLogger(__name__)

# Below this, an answer is not trusted. Tuned on the benchmark decks.
JUDGE_MIN_CONFIDENCE = 0.8

NONE = "NONE"
NO_TARGET = "NO_TARGET"


class Status(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ReasonKind(StrEnum):
    BLOCKLISTED = "blocklisted"
    UNREADABLE = "unreadable"
    NONE_FITS = "none"
    UNSTABLE = "unstable"
    LOW_CONFIDENCE = "low_confidence"
    INVALID = "invalid"
    NOT_ASKED = "not_asked"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CompiledCard:
    name: str
    facts: CardFacts | None  # None when Scryfall doesn't know the card
    status: str
    program: dict | None = None
    reason: str = ""
    reason_kind: str = ""
    model: str = ""

    @property
    def is_instant_or_sorcery(self) -> bool:
        return self.facts is not None and bool({"Instant", "Sorcery"} & set(self.facts.types))


@dataclass(frozen=True)
class DeckCard:
    quantity: int
    is_commander: bool
    card: CompiledCard


class _Refusal(Exception):
    def __init__(self, kind: ReasonKind, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def compile_card(card_json: dict, cache, *, ask=None) -> CompiledCard:
    """Compiles one card (raw Scryfall JSON), through the ``card_rules`` cache.

    ``ask(state, questions) -> {"model", "answers"}`` defaults to Jev. Never
    raises: a card that can't be compiled is UNSUPPORTED, with a reason.
    """
    facts = characteristics(card_json)
    # A card without an oracle id would share its cache entry with every other.
    cacheable = bool(facts.oracle_id)
    cached = cache.get_rule(facts.oracle_id, DSL_VERSION) if cacheable else None
    if cached is not None:
        return CompiledCard(facts.name, facts, **cached)

    compiled = _compile(facts, ask or typesafe.system_one)
    # Only Jev's answers are worth keeping: everything else is recomputed for
    # free, so a fix to the pre-pass or the splitter applies at once. A card
    # Jev couldn't be asked about is retried next time.
    if cacheable and compiled.model and compiled.reason_kind != ReasonKind.NOT_ASKED:
        cache.set_rule(
            facts.oracle_id,
            DSL_VERSION,
            {
                "program": compiled.program,
                "status": compiled.status,
                "reason": compiled.reason,
                "reason_kind": compiled.reason_kind,
                "model": compiled.model,
            },
        )
    return compiled


def compile_deck(entries: list[dict], cache, *, ask=None, fetch=fetch_card_raw) -> list[DeckCard]:
    """Compiles every ``{"name", "quantity", "is_commander"}`` entry of a deck."""
    compiled: dict[str, CompiledCard] = {}
    cards = []
    for entry in entries:
        name = entry["name"]
        if name not in compiled:
            raw = fetch(name, cache)
            if raw is None:
                compiled[name] = CompiledCard(
                    name,
                    None,
                    Status.UNSUPPORTED,
                    reason="not found on Scryfall",
                    reason_kind=ReasonKind.NOT_FOUND,
                )
            else:
                compiled[name] = compile_card(raw, cache, ask=ask)
        cards.append(DeckCard(entry["quantity"], entry["is_commander"], compiled[name]))
    return cards


def card_state(facts: CardFacts) -> dict:
    """What Jev sees about a card: only what the questions need."""
    return {"card": facts.name, "type_line": facts.type_line, "rules_text": facts.residual}


def build_questions(abilities: list[AbilityText]) -> dict:
    """Every question about every clause of a card, for one request."""
    ops = {name: spec.description for name, spec in OPS.items()} | {
        NONE: "None of these: the clause does something else, or more than one of these things."
    }
    # Jev leans toward the first option: asking again in reverse order shows
    # whether the answer depends on the order.
    reversed_ops = dict(reversed(list(ops.items())))
    targets = {name: description for name, (_, description) in SELECTORS.items()} | {
        NO_TARGET: "The clause names no player and no object.",
        NONE: "None of these: the clause affects a different set of players or objects.",
    }
    reversed_targets = dict(reversed(list(targets.items())))
    questions = {}
    for key, clause in _clauses(abilities):
        questions[f"op_{key}"] = _choice(
            clause, "Which single operation does `clause` perform?", ops
        )
        questions[f"op_{key}_rev"] = _choice(
            clause, "Which single operation does `clause` perform?", reversed_ops
        )
        if clause.has_target:
            questions[f"target_{key}"] = _choice(
                clause, "Which player or object does `clause` affect?", targets
            )
            questions[f"target_{key}_rev"] = _choice(
                clause, "Which player or object does `clause` affect?", reversed_targets
            )
    return questions


def assemble(abilities: list[AbilityText], answers: dict) -> dict:
    """Builds the program payload from the clause structure and Jev's answers."""
    payload = []
    clause_no = 0
    for a_idx, ability in enumerate(abilities):
        targets: list[dict] = []
        ops: list[dict] = []
        for c_idx, clause in enumerate(ability.clauses):
            clause_no += 1
            key = f"{a_idx}_{c_idx}"
            where = f"clause {clause_no} ({clause.text!r})"
            op = _answer(answers, f"op_{key}", where)
            reverse = _answer(answers, f"op_{key}_rev", where)
            if NONE in (op, reverse):
                raise _Refusal(ReasonKind.NONE_FITS, f"{where}: no operation of the DSL fits")
            if op != reverse:
                raise _Refusal(
                    ReasonKind.UNSTABLE,
                    f"{where}: {op} or {reverse}, depending on the option order",
                )
            spec = OPS[op]
            entry = {"op": op}
            if "target" in spec.fields:
                entry["target"] = _target(clause, key, spec, answers, targets, ops, where)
            for field in sorted(spec.fields - {"target"}):
                entry[field] = _field(field, op, clause, key, answers, where)
            ops.append(entry)
        payload.append(
            {
                "trigger": ability.trigger,
                "cost": ability.cost,
                "mode_group": ability.mode_group,
                "targets": targets,
                "ops": ops,
            }
        )
    return {"abilities": payload}


def _compile(facts: CardFacts, ask) -> CompiledCard:
    supported = Status.PARTIAL if facts.partial else Status.SUPPORTED
    if facts.residue is Residue.EMPTY:
        return CompiledCard(facts.name, facts, supported, {"abilities": []})
    if facts.residue is Residue.BLOCKED:
        return _unsupported(facts, ReasonKind.BLOCKLISTED, f"blocklisted: {facts.blocked_by}")
    try:
        abilities = split_abilities(facts.residual, facts)
    except Unreadable as exc:
        return _unsupported(facts, ReasonKind.UNREADABLE, str(exc))
    questions = build_questions(abilities)
    try:
        reply = ask(card_state(facts), questions)
    except typesafe.TypeSafeError as exc:
        logger.warning("Judge could not ask Jev about %s: %s", facts.name, exc)
        return _unsupported(facts, ReasonKind.NOT_ASKED, str(exc))
    model = reply["model"]
    try:
        _check_choices(questions, reply["answers"])
        payload = assemble(abilities, reply["answers"])
    except _Refusal as exc:
        return _unsupported(facts, exc.kind, exc.reason, model)
    except (LookupError, TypeError, ValueError) as exc:
        # An answer outside the options or missing a field: a service fault,
        # not a verdict on the card, so it is retried next time.
        logger.warning("Judge got a malformed Jev answer for %s: %r", facts.name, exc)
        return _unsupported(facts, ReasonKind.NOT_ASKED, f"malformed Jev answer: {exc!r}", model)
    try:
        validate_program(payload, facts)
    except DslError as exc:
        return _unsupported(facts, ReasonKind.INVALID, str(exc), model)
    return CompiledCard(facts.name, facts, supported, payload, model=model)


def _unsupported(facts, kind, reason, model="") -> CompiledCard:
    return CompiledCard(facts.name, facts, Status.UNSUPPORTED, None, reason, kind, model)


def _clauses(abilities):
    for a_idx, ability in enumerate(abilities):
        for c_idx, clause in enumerate(ability.clauses):
            yield f"{a_idx}_{c_idx}", clause


def _choice(clause: Clause, question: str, criteria: dict) -> dict:
    return {
        "type": "choice",
        "instructions": {"clause": clause.text, "question": question},
        "criteria": criteria,
    }


def _check_choices(questions: dict, answers: dict) -> None:
    for key, question in questions.items():
        if answers[key]["choice"] not in question["criteria"]:
            raise ValueError(f"{key}: {answers[key]['choice']!r} is not one of the options")


def _answer(answers: dict, key: str, where: str) -> str:
    answer = answers[key]
    if answer["confidence"] < JUDGE_MIN_CONFIDENCE:
        raise _Refusal(
            ReasonKind.LOW_CONFIDENCE,
            f"{where}: {key.split('_')[0]} answered with confidence {answer['confidence']:.2f}",
        )
    return answer["choice"]


def _target(clause, key, spec, answers, targets, ops, where) -> str:
    if clause.inherits_target:
        if not ops or "target" not in ops[-1]:
            raise _Refusal(ReasonKind.UNREADABLE, f"{where}: continues a clause that has no target")
        return ops[-1]["target"]
    selector = NO_TARGET
    if clause.has_target:
        selector = _answer(answers, f"target_{key}", where)
        reverse = _answer(answers, f"target_{key}_rev", where)
        if NONE in (selector, reverse):
            raise _Refusal(ReasonKind.NONE_FITS, f"{where}: no selector of the DSL fits")
        if selector != reverse:
            raise _Refusal(
                ReasonKind.UNSTABLE,
                f"{where}: {selector} or {reverse}, depending on the option order",
            )
    if selector == NO_TARGET:
        if spec.target_kinds != {"player"}:
            raise _Refusal(
                ReasonKind.UNREADABLE, f"{where}: names nothing for the operation to affect"
            )
        # "Draw a card", "Mill three cards": a player operation with no
        # subject is the controller's.
        selector = "YOU"
    target_id = f"t{len(targets) + 1}"
    targets.append({"id": target_id, "selector": selector, "restriction": clause.restriction})
    return target_id


def _field(field, op, clause, key, answers, where):
    if field == "amount":
        # The splitter refuses clauses with several numbers: at most one here.
        return _only(clause.amounts, "amount", where)
    if field in ("power", "toughness"):
        pairs = clause.token_pts if op == "CREATE_TOKEN" else clause.pt_mods
        return _only(pairs, "power/toughness", where)[0 if field == "power" else 1]
    if field == "duration":
        return clause.duration
    candidates = {
        "color": clause.colors,
        "keyword": clause.keywords,
        "counter": clause.counters,
        "card_type": clause.card_types,
        "token_name": clause.token_names,
    }[field]
    return _only(candidates, field.replace("_", " "), where)


def _only(values, what, where):
    if len(values) != 1:
        raise _Refusal(ReasonKind.UNREADABLE, f"{where}: {len(values)} candidates for the {what}")
    return values[0]
