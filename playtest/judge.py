"""The judge: compiles a card's residual rules text into a DSL program.

One Jev request per card. The state is the card's name, type line and
residual text, nothing else; the questions, evaluated in parallel, ask for
every clause which operation it performs and which selector it affects — each
twice, with the options in forward and reverse order, because Jev leans toward
the first option and a disagreement means the answer is not stable.

The program is then assembled in code: the op's fields are read off the clause
by the op's grammar (:func:`~playtest.engine.dsl.parse_clause`), the selector
Jev named must be the one the text states, and the result goes through
:func:`~playtest.engine.dsl.validate_program`. Anything short of that makes
the card UNSUPPORTED with a reason naming the clause: a refusal is a feature,
an approximation is a bug. There is no repair round-trip.

The judge never raises into its caller. A card Jev could not be asked about
(no key, network, rate limit) is UNSUPPORTED and *not* cached, so it is retried
next time; a card Jev answered is cached for good, keyed by oracle id and DSL
version.
"""

import logging
from dataclasses import dataclass, field

from decks.scryfall import fetch_card_raw
from decks.text_utils import card_slug, front_face_name

from .agents import typesafe
from .engine import dsl
from .engine.cardrules import CardFacts, ResidualClass, characteristics
from .engine.clauses import ClauseSet, SplitError, split_abilities
from .engine.coverage import CoverageEntry, ReasonKind, Status, coverage_report

logger = logging.getLogger(__name__)

# Answers below this confidence make the card UNSUPPORTED; changing it does
# not change the DSL version.
JUDGE_MIN_CONFIDENCE = 0.8

STATE_TEMPLATE = "{name}\n{type_line}\n\n{residual}"

OP_INSTRUCTIONS = (
    "The state is a Magic: The Gathering card and the part of its rules text still to be "
    'understood. Consider only this clause of it: "{clause}". '
    "Which single operation does this clause perform?"
)
TARGET_INSTRUCTIONS = (
    "The state is a Magic: The Gathering card and the part of its rules text still to be "
    'understood. Consider only this clause of it: "{clause}". '
    "Which player or object does this clause affect?"
)


@dataclass(frozen=True)
class CompiledCard:
    name: str
    status: str
    facts: CardFacts | None = None
    # The validated program as persisted (dsl.to_json), or None when unsupported.
    program: dict | None = None
    reason: str = ""
    reason_kind: str = ""
    # The versioned Jev id that answered, when Jev was asked.
    model: str = ""
    # Whether a Jev request was made to compile it (not on a cache hit).
    asked: bool = False

    @property
    def supported(self) -> bool:
        return self.status in (Status.SUPPORTED, Status.PARTIAL)

    def card_program(self) -> dsl.CardProgram | None:
        if self.program is None or self.facts is None:
            return None
        return dsl.from_json(self.program, self.facts)


@dataclass(frozen=True)
class DeckEntry:
    quantity: int
    is_commander: bool
    card: CompiledCard
    raw: dict = field(default_factory=dict, compare=False)

    def coverage(self) -> CoverageEntry:
        facts = self.card.facts
        return CoverageEntry(
            name=self.card.name,
            quantity=self.quantity,
            is_commander=self.is_commander,
            status=self.card.status,
            reason_kind=self.card.reason_kind,
            reason=self.card.reason,
            spell=bool(facts and facts.is_instant_or_sorcery),
        )


class _Refused(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def _unsupported(facts: CardFacts, kind: str, reason: str, **extra) -> CompiledCard:
    return CompiledCard(facts.name, Status.UNSUPPORTED, facts, None, reason, kind, **extra)


def _supported(facts: CardFacts, program: dict, **extra) -> CompiledCard:
    status = Status.PARTIAL if facts.partial else Status.SUPPORTED
    reason = "only the front face is played" if facts.partial else ""
    return CompiledCard(facts.name, status, facts, program, reason, "", **extra)


def _flat_clauses(abilities: list[ClauseSet]):
    index = 0
    for a_index, ability in enumerate(abilities):
        for c_index, clause in enumerate(ability.clauses):
            yield index, a_index, c_index, clause
            index += 1


def build_questions(abilities: list[ClauseSet]) -> dict:
    """The questions of the one request a card needs."""
    ops = {name: spec.description for name, spec in dsl.OPS.items()}
    ops[dsl.NONE_OPTION] = dsl.NONE_OP_DESCRIPTION
    selectors = {name: spec.description for name, spec in dsl.SELECTORS.items()}
    selectors[dsl.NONE_OPTION] = dsl.NONE_SELECTOR_DESCRIPTION
    questions = {}
    for index, _, _, clause in _flat_clauses(abilities):
        op_text = OP_INSTRUCTIONS.format(clause=clause.text)
        questions[f"op_{index}"] = typesafe.choice(op_text, ops)
        questions[f"op_{index}_rev"] = typesafe.choice(op_text, dict(reversed(list(ops.items()))))
        if clause.target_phrases and clause.shares_target_with is None:
            target_text = TARGET_INSTRUCTIONS.format(clause=clause.text)
            questions[f"target_{index}"] = typesafe.choice(target_text, selectors)
            questions[f"target_{index}_rev"] = typesafe.choice(
                target_text, dict(reversed(list(selectors.items())))
            )
    return questions


def _stable_answer(answers: dict, key: str, options, clause_no: int, what: str) -> str:
    """Reads a forward/reverse pair of answers; raises :class:`_Refused` unless they agree."""
    try:
        forward, conf_f, _ = typesafe.read_choice(answers.get(key), options)
        reverse, conf_r, _ = typesafe.read_choice(answers.get(f"{key}_rev"), options)
    except typesafe.TypeSafeError as exc:
        raise _Refused(ReasonKind.INVALID, f"clause {clause_no}: {exc}") from exc
    if forward != reverse:
        raise _Refused(
            ReasonKind.UNSTABLE,
            f"clause {clause_no}: the {what} answer changed with the option order ({forward} / {reverse})",
        )
    if forward == dsl.NONE_OPTION:
        raise _Refused(ReasonKind.NONE, f"clause {clause_no}: no {what} in the DSL fits")
    confidence = min(conf_f, conf_r)
    if confidence < JUDGE_MIN_CONFIDENCE:
        raise _Refused(
            ReasonKind.LOW_CONFIDENCE,
            f"clause {clause_no}: {what} {forward} at confidence {confidence:.2f}",
        )
    return forward


def assemble(abilities: list[ClauseSet], answers: dict) -> dict:
    """Builds the program payload from the clause structure and Jev's answers.

    Raises :class:`_Refused` naming the clause that cannot be assembled.
    """
    op_options = [*dsl.OPS, dsl.NONE_OPTION]
    selector_options = [*dsl.SELECTORS, dsl.NONE_OPTION]
    program_abilities = []
    flat = list(_flat_clauses(abilities))
    for a_index, ability in enumerate(abilities):
        targets: dict[str, dict] = {}
        clause_targets: dict[int, str] = {}
        ops = []
        for index, owner, c_index, clause in flat:
            if owner != a_index:
                continue
            clause_no = index + 1
            op = _stable_answer(answers, f"op_{index}", op_options, clause_no, "operation")
            fields = dsl.parse_clause(op, clause.text)
            if fields is None:
                raise _Refused(ReasonKind.INVALID, f"clause {clause_no}: “{clause.text}” does not read as {op}")
            entry = {"op": op}
            selector = fields.pop("selector", None)
            restriction = fields.pop("restriction", None)
            referent = _referent(targets, clause_targets)
            if op == "DEAL_DAMAGE" and clause.text.startswith("it ") and (
                referent is not None or ability.trigger == dsl.Trigger.SPELL
            ):
                # "it deals" is this permanent only when nothing was targeted before.
                raise _Refused(ReasonKind.INVALID, f"clause {clause_no}: “it” is ambiguous")
            if fields.get("amount") in dsl.EXPRESSIONS.values() or selector in dsl.DERIVED_SELECTORS:
                if referent is None:
                    raise _Refused(ReasonKind.INVALID, f"clause {clause_no}: “it” refers to nothing targeted before")
                if fields.get("amount") in dsl.EXPRESSIONS.values():
                    fields["of"] = referent
            if selector is not None:
                if clause.shares_target_with is not None:
                    key = clause_targets.get(clause.shares_target_with)
                    if key is None or targets[key]["selector"] != selector:
                        raise _Refused(ReasonKind.INVALID, f"clause {clause_no}: unclear subject")
                else:
                    if clause.target_phrases:
                        named = _stable_answer(answers, f"target_{index}", selector_options, clause_no, "target")
                        if named != selector:
                            raise _Refused(
                                ReasonKind.INVALID,
                                f"clause {clause_no}: Jev read the target as {named}, the text says {selector}",
                            )
                    key = _target_key(targets, selector, restriction, referent if selector in dsl.DERIVED_SELECTORS else None)
                clause_targets[c_index] = key
                entry["target"] = key
            entry.update(fields)
            ops.append(entry)
        data = {"trigger": ability.trigger, "ops": ops, "text": ability.line}
        if targets:
            data["targets"] = targets
        if ability.cost is not None:
            data["cost"] = {
                "mana": ability.cost.mana,
                "tap": ability.cost.tap,
                "sacrifice_self": ability.cost.sacrifice_self,
                "life": ability.cost.life,
                "discard": ability.cost.discard,
            }
        if ability.mode_group is not None:
            data["mode_group"] = ability.mode_group
        if ability.when:
            data["when"] = ability.when
        program_abilities.append(data)
    return {"version": dsl.DSL_VERSION, "abilities": program_abilities}


def _referent(targets: dict, clause_targets: dict[int, str]) -> str | None:
    """The target "it" / "its" points back to: the latest targeted object of the ability."""
    for c_index in sorted(clause_targets, reverse=True):
        entry = targets[clause_targets[c_index]]
        spec = dsl.SELECTORS[entry["selector"]]
        if spec.targeted and spec.kind != dsl.SelKind.PLAYER:
            return clause_targets[c_index]
    return None


def _target_key(targets: dict, selector: str, restriction: dict | None, of: str | None = None) -> str:
    entry = {"selector": selector}
    if of is not None:
        entry["of"] = of
    if restriction is not None:
        entry["restriction"] = restriction
    # Untargeted selectors ("you", "each opponent") are the same set every time.
    if not dsl.SELECTORS[selector].targeted:
        for key, existing in targets.items():
            if existing == entry:
                return key
    key = f"t{len(targets) + 1}"
    targets[key] = entry
    return key


class _NotCached(Exception):
    """A cache-only compilation met a card that needs the judge."""


def compile_card(card: dict, cache, *, ask=None, cached_only: bool = False) -> CompiledCard:
    """Compiles one card from its raw Scryfall JSON; never raises.

    ``cache`` exposes ``get_rule``/``set_rule`` (see :mod:`decks.cache`).
    ``ask`` defaults to :func:`typesafe.system_one`. With ``cached_only`` the
    judge is never asked: a card that would need it raises ``_NotCached``.
    """
    try:
        facts = characteristics(card)
    except Exception as exc:  # noqa: BLE001 - a malformed card degrades, never raises.
        logger.warning("Could not read card %s: %s", card.get("name"), exc)
        return CompiledCard(card.get("name") or "Unknown card", Status.UNSUPPORTED, None, None,
                            f"unreadable card data: {exc}", ReasonKind.UNREADABLE)

    if facts.residual_class == ResidualClass.EMPTY:
        return _supported(facts, {"version": dsl.DSL_VERSION, "abilities": []})
    if facts.residual_class == ResidualClass.BLOCKED:
        return _unsupported(facts, ReasonKind.BLOCKLISTED, facts.block_reason)
    try:
        abilities = split_abilities(facts.residual, facts)
    except SplitError as exc:
        return _unsupported(facts, ReasonKind.UNREADABLE, str(exc))

    cached = cache.get_rule(facts.oracle_id, dsl.DSL_VERSION)
    if cached is not None:
        return _from_cache(facts, cached)

    if cached_only:
        raise _NotCached(facts.name)
    ask = ask or typesafe.system_one
    state = STATE_TEMPLATE.format(name=facts.name, type_line=facts.type_line, residual=facts.residual)
    try:
        response = ask(state, build_questions(abilities))
    except typesafe.TypeSafeError as exc:
        logger.warning("Judge could not ask about %s: %s", facts.name, exc)
        return _unsupported(facts, ReasonKind.NOT_ASKED, f"the judge could not be reached: {exc}")
    model = response.get("model", "")

    try:
        payload = assemble(abilities, response.get("answers") or {})
        program = dsl.validate_program(payload, facts)
    except _Refused as exc:
        result = _unsupported(facts, exc.kind, exc.reason, model=model, asked=True)
    except dsl.DslError as exc:
        result = _unsupported(facts, ReasonKind.INVALID, f"invalid program: {exc}", model=model, asked=True)
    else:
        result = _supported(facts, dsl.to_json(program), model=model, asked=True)

    cache.set_rule(
        facts.oracle_id,
        dsl.DSL_VERSION,
        program=result.program,
        status=result.status,
        reason=result.reason,
        reason_kind=result.reason_kind,
        model=model,
    )
    return result


def _from_cache(facts: CardFacts, cached: dict) -> CompiledCard:
    program = cached.get("program")
    if program is not None:
        try:
            dsl.validate_program(program, facts)
        except dsl.DslError as exc:
            return _unsupported(facts, ReasonKind.INVALID, f"cached program is invalid: {exc}")
        return _supported(facts, program, model=cached.get("model", ""))
    return _unsupported(
        facts,
        cached.get("reason_kind") or ReasonKind.INVALID,
        cached.get("reason") or "",
        model=cached.get("model", ""),
    )


def raw_from_stored(data: dict) -> dict:
    """A best-effort raw card from the processed card a deck stores.

    Used when the raw Scryfall JSON is missing from the cache: the processed
    card lacks ``keywords``, so keyword lines then stay in the residual text.
    """
    face = (data.get("faces") or [{}])[0]
    return {
        "name": data.get("name"),
        "oracle_id": data.get("id") or data.get("name"),
        "type_line": face.get("type_line") or data.get("type_line", ""),
        "mana_cost": face.get("mana_cost", ""),
        "cmc": data.get("cmc", 0),
        "oracle_text": face.get("rules_text", ""),
        "power": data.get("power"),
        "toughness": data.get("toughness"),
        "colors": data.get("color_identity", []),
        "produced_mana": data.get("produced_mana", []),
        "keywords": [],
    }


def compile_deck(cards: list, cache, *, ask=None, fetch=None) -> list[DeckEntry]:
    """Compiles every card of a deck (``Deck.cards`` shape); never raises.

    The raw Scryfall JSON is read through ``fetch`` (the cache first, so an
    imported deck costs no Scryfall call); each distinct card is compiled once.
    """
    fetch = fetch or fetch_card_raw
    compiled: dict[str, tuple[CompiledCard, dict]] = {}
    entries = []
    for item in cards:
        data = item.get("data") or {}
        name = data.get("name") or "Unknown card"
        if name not in compiled:
            raw = None
            try:
                raw = fetch(name, cache)
            except Exception as exc:  # noqa: BLE001 - fall back to the stored card.
                logger.warning("Could not fetch raw data for %s: %s", name, exc)
            raw = raw or raw_from_stored(data)
            compiled[name] = (compile_card(raw, cache, ask=ask), raw)
        card, raw = compiled[name]
        entries.append(DeckEntry(int(item.get("quantity") or 1), bool(item.get("is_commander")), card, raw))
    return entries


def cached_coverage(cards: list, cache) -> dict | None:
    """A deck's coverage from the caches alone, or None when it is not all there.

    For a page view: it never asks the judge and never calls Scryfall. The raw
    card must be cached and every card that needs the judge must already have
    a compiled rule.
    """
    entries = []
    for item in cards:
        name = (item.get("data") or {}).get("name") or ""
        raw = cache.get_card(f"card_en_{card_slug(front_face_name(name))}")
        if raw is None or raw.get("error"):
            return None
        try:
            compiled = compile_card(raw, cache, cached_only=True)
        except _NotCached:
            return None
        entries.append(DeckEntry(int(item.get("quantity") or 1), bool(item.get("is_commander")), compiled).coverage())
    return coverage_report(entries)
