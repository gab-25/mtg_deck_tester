"""The effect DSL: a compiled card is data, from a closed vocabulary.

A program is a list of abilities, each binding a trigger to a sequence of
operations over declared targets. The persisted form is the validated dict
(``{"abilities": [...]}``), never these classes, so refactoring them never
invalidates the ``card_rules`` cache. :func:`validate_program` is the only way
in: it rejects, never coerces.
"""

import re
from dataclasses import dataclass

from .cardrules import SUPPORTED_KEYWORDS, CardFacts
from .manacost import ManaCost, ManaCostError, parse_mana_cost

# Bump only when the accepted vocabulary or a program's meaning changes —
# never for engine bugfixes, criteria wording or threshold tweaks.
DSL_VERSION = "1"

TRIGGERS = (
    "SPELL",  # an instant's or sorcery's effect
    "STATIC",  # a permanent's continuous effect
    "ACTIVATED",
    "ETB",
    "DIES",
    "ATTACK_TRIGGER",
    "UPKEEP_TRIGGER",
    "END_STEP_TRIGGER",
)

# Selector -> (kind, description). The description is what Jev reads as the option.
SELECTORS = {
    "YOU": ("player", "You, the controller of this card."),
    "TARGET_PLAYER": ("player", "One target player, who may be you or an opponent."),
    "TARGET_OPPONENT": ("player", "One target opponent."),
    "EACH_PLAYER": ("player", "Every player, you included."),
    "EACH_OPPONENT": ("player", "Every opponent of yours."),
    "ANY_TARGET": ("any", "Any target: one creature, player or planeswalker."),
    "TARGET_CREATURE": ("object", "One target creature, whoever controls it."),
    "TARGET_CREATURE_YOU_CONTROL": ("object", "One target creature you control."),
    "TARGET_CREATURE_OPPONENT_CONTROLS": ("object", "One target creature an opponent controls."),
    "TARGET_ARTIFACT": ("object", "One target artifact."),
    "TARGET_ENCHANTMENT": ("object", "One target enchantment."),
    "TARGET_ARTIFACT_OR_ENCHANTMENT": ("object", "One target artifact or enchantment."),
    "TARGET_LAND": ("object", "One target land."),
    "TARGET_NONLAND_PERMANENT": ("object", "One target nonland permanent."),
    "TARGET_PERMANENT": ("object", "One target permanent of any type."),
    "EACH_CREATURE": ("object", "Every creature on the battlefield."),
    "EACH_CREATURE_YOU_CONTROL": ("object", "Every creature you control."),
    "EACH_CREATURE_OPPONENTS_CONTROL": ("object", "Every creature your opponents control."),
    "SELF": ("object", "This permanent itself."),
    "TARGET_SPELL": ("spell", "One target spell on the stack."),
    "TARGET_CREATURE_SPELL": ("spell", "One target creature spell on the stack."),
    "TARGET_NONCREATURE_SPELL": ("spell", "One target noncreature spell on the stack."),
}

RESTRICTIONS = ("POWER_LE", "POWER_GE", "MV_LE", "MV_GE")
COUNTERS = ("+1/+1", "-1/-1")
DURATIONS = ("END_OF_TURN", "PERMANENT")
CARD_TYPES = ("CREATURE", "ARTIFACT", "ENCHANTMENT", "LAND", "PERMANENT")
MANA_COLORS = ("W", "U", "B", "R", "G", "C")

MAX_ABILITIES = 12
MAX_OPS = 6
MAX_AMOUNT = 20


@dataclass(frozen=True, slots=True)
class OpSpec:
    description: str
    fields: frozenset[str]
    target_kinds: frozenset[str] = frozenset()


def _op(description, *fields, targets=()):
    return OpSpec(description, frozenset(fields), frozenset(targets))


_PLAYER = ("player",)
_OBJECT = ("object",)

# The closed vocabulary. The description is what Jev reads as the option.
OPS = {
    "DEAL_DAMAGE": _op(
        "Deals an amount of damage.", "target", "amount", targets=("any", "object", "player")
    ),
    "GAIN_LIFE": _op("A player gains an amount of life.", "target", "amount", targets=_PLAYER),
    "LOSE_LIFE": _op("A player loses an amount of life.", "target", "amount", targets=_PLAYER),
    "DRAW_CARDS": _op("A player draws a number of cards.", "target", "amount", targets=_PLAYER),
    "DISCARD_CARDS": _op(
        "A player discards a number of cards.", "target", "amount", targets=_PLAYER
    ),
    "DESTROY": _op("Destroys permanents.", "target", targets=_OBJECT),
    "EXILE": _op("Exiles permanents.", "target", targets=_OBJECT),
    "RETURN_TO_HAND": _op("Returns permanents to their owner's hand.", "target", targets=_OBJECT),
    "TAP": _op("Taps permanents.", "target", targets=_OBJECT),
    "UNTAP": _op("Untaps permanents.", "target", targets=_OBJECT),
    "ADD_MANA": _op("Adds mana to your mana pool.", "amount", "color"),
    "CREATE_TOKEN": _op(
        "Creates creature tokens with a given power and toughness.",
        "target",
        "amount",
        "power",
        "toughness",
        "token_name",
        targets=_PLAYER,
    ),
    "MODIFY_PT": _op(
        "Raises or lowers the power and toughness of creatures.",
        "target",
        "power",
        "toughness",
        "duration",
        targets=_OBJECT,
    ),
    "GRANT_KEYWORD": _op(
        "Gives creatures a keyword ability such as flying.",
        "target",
        "keyword",
        "duration",
        targets=_OBJECT,
    ),
    "PUT_COUNTERS": _op(
        "Puts +1/+1 or -1/-1 counters on creatures.", "target", "amount", "counter", targets=_OBJECT
    ),
    "SACRIFICE": _op(
        "A player sacrifices permanents.", "target", "amount", "card_type", targets=_PLAYER
    ),
    "MILL": _op(
        "A player mills cards: puts them from library into graveyard.",
        "target",
        "amount",
        targets=_PLAYER,
    ),
    "COUNTER_SPELL": _op("Counters a spell on the stack.", "target", targets=("spell",)),
}

_PROGRAM_KEYS = {"abilities"}
_ABILITY_KEYS = {"trigger", "cost", "mode_group", "targets", "ops"}
_COST_KEYS = {"mana", "tap", "sacrifice_self"}
_TARGET_KEYS = {"id", "selector", "restriction"}
_RESTRICTION_KEYS = {"kind", "value"}

_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_MANA_RUN = re.compile(r"(?:\{[WUBRGC]\})+")


class DslError(ValueError):
    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class Restriction:
    kind: str
    value: int


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    selector: str
    restriction: Restriction | None = None


@dataclass(frozen=True, slots=True)
class Op:
    kind: str
    target: str | None = None
    amount: int | None = None
    color: str | None = None
    power: int | None = None
    toughness: int | None = None
    keyword: str | None = None
    counter: str | None = None
    duration: str | None = None
    token_name: str | None = None
    card_type: str | None = None


@dataclass(frozen=True, slots=True)
class Cost:
    mana: ManaCost
    tap: bool
    sacrifice_self: bool


@dataclass(frozen=True, slots=True)
class Ability:
    trigger: str
    ops: tuple[Op, ...]
    targets: tuple[Target, ...] = ()
    cost: Cost | None = None
    mode_group: int | None = None


@dataclass(frozen=True, slots=True)
class CardProgram:
    abilities: tuple[Ability, ...]


def grounded_numbers(oracle_text: str) -> frozenset[int]:
    """Every number the oracle text states: digits, English words, runs of mana symbols."""
    found = {int(d) for d in re.findall(r"\d+", oracle_text)}
    found |= {
        _NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", oracle_text.lower()) if w in _NUMBER_WORDS
    }
    found |= {run.group(0).count("{") for run in _MANA_RUN.finditer(oracle_text)}
    return frozenset(found)


def validate_program(payload, facts: CardFacts) -> CardProgram:
    """Checks a program against the vocabulary and the card; raises :class:`DslError`."""
    _object(payload, "program", _PROGRAM_KEYS, _PROGRAM_KEYS)
    abilities = payload["abilities"]
    if not isinstance(abilities, list) or len(abilities) > MAX_ABILITIES:
        raise DslError("abilities", f"must be a list of at most {MAX_ABILITIES}")
    grounded = grounded_numbers(facts.oracle_text)
    return CardProgram(
        tuple(_ability(a, f"abilities[{i}]", grounded) for i, a in enumerate(abilities))
    )


def _object(value, path, allowed, required):
    if not isinstance(value, dict):
        raise DslError(path, "must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise DslError(path, f"unknown key {min(unknown)!r}")
    missing = required - set(value)
    if missing:
        raise DslError(path, f"missing key {min(missing)!r}")


def _enum(value, domain, path):
    if value not in domain:
        raise DslError(path, f"{value!r} is not one of {', '.join(domain)}")
    return value


def _int(value, low, high, path):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise DslError(path, f"must be an integer between {low} and {high}")
    return value


def _ability(data, path, grounded) -> Ability:
    _object(data, path, _ABILITY_KEYS, _ABILITY_KEYS)
    trigger = _enum(data["trigger"], TRIGGERS, f"{path}.trigger")
    cost = _cost(data["cost"], trigger, f"{path}.cost")
    mode_group = data["mode_group"]
    if mode_group is not None:
        _int(mode_group, 1, MAX_ABILITIES, f"{path}.mode_group")

    if not isinstance(data["targets"], list):
        raise DslError(f"{path}.targets", "must be a list")
    targets = tuple(
        _target(t, f"{path}.targets[{i}]", grounded) for i, t in enumerate(data["targets"])
    )
    by_id = {t.id: t for t in targets}
    if len(by_id) != len(targets):
        raise DslError(f"{path}.targets", "duplicate target id")

    ops_data = data["ops"]
    if not isinstance(ops_data, list) or not 1 <= len(ops_data) <= MAX_OPS:
        raise DslError(f"{path}.ops", f"must be a list of 1 to {MAX_OPS} operations")
    ops = tuple(_operation(o, f"{path}.ops[{i}]", by_id, grounded) for i, o in enumerate(ops_data))

    unused = set(by_id) - {op.target for op in ops}
    if unused:
        raise DslError(f"{path}.targets", f"target {min(unused)!r} is declared but never used")
    if trigger == "ACTIVATED" and any(op.kind == "ADD_MANA" for op in ops):
        if targets or any(op.kind != "ADD_MANA" for op in ops):
            raise DslError(path, "a mana ability can only add mana")
    return Ability(trigger, ops, targets, cost, mode_group)


def _cost(data, trigger, path) -> Cost | None:
    if trigger != "ACTIVATED":
        if data is not None:
            raise DslError(path, "only an activated ability has a cost")
        return None
    if data is None:
        raise DslError(path, "an activated ability needs a cost")
    _object(data, path, _COST_KEYS, _COST_KEYS)
    if not isinstance(data["mana"], str):
        raise DslError(f"{path}.mana", "must be a mana cost string")
    try:
        mana = parse_mana_cost(data["mana"])
    except ManaCostError as exc:
        raise DslError(f"{path}.mana", str(exc)) from exc
    if not isinstance(data["tap"], bool) or not isinstance(data["sacrifice_self"], bool):
        raise DslError(path, "tap and sacrifice_self must be booleans")
    if mana == ManaCost() and not data["tap"] and not data["sacrifice_self"]:
        # A free, repeatable ability is an infinite loop waiting to happen.
        raise DslError(path, "an activated ability with no cost")
    return Cost(mana, data["tap"], data["sacrifice_self"])


def _target(data, path, grounded) -> Target:
    _object(data, path, _TARGET_KEYS, _TARGET_KEYS)
    if not isinstance(data["id"], str) or not data["id"]:
        raise DslError(f"{path}.id", "must be a non-empty string")
    selector = _enum(data["selector"], tuple(SELECTORS), f"{path}.selector")
    restriction = None
    if data["restriction"] is not None:
        if SELECTORS[selector][0] not in ("object", "any"):
            raise DslError(f"{path}.restriction", f"{selector} can't be restricted")
        rpath = f"{path}.restriction"
        _object(data["restriction"], rpath, _RESTRICTION_KEYS, _RESTRICTION_KEYS)
        kind = _enum(data["restriction"]["kind"], RESTRICTIONS, f"{rpath}.kind")
        value = _grounded(
            _int(data["restriction"]["value"], 0, MAX_AMOUNT, f"{rpath}.value"), grounded, rpath
        )
        restriction = Restriction(kind, value)
    return Target(data["id"], selector, restriction)


def _grounded(value, grounded, path):
    if abs(value) not in grounded:
        raise DslError(path, f"{value} does not appear in the oracle text")
    return value


def _operation(data, path, targets, grounded) -> Op:
    if not isinstance(data, dict):
        raise DslError(path, "must be an object")
    kind = _enum(data.get("op"), tuple(OPS), f"{path}.op")
    spec = OPS[kind]
    _object(data, path, spec.fields | {"op"}, spec.fields | {"op"})
    fields = {}
    if "target" in spec.fields:
        ref = data["target"]
        if ref not in targets:
            raise DslError(f"{path}.target", f"{ref!r} is not a declared target")
        selector_kind = SELECTORS[targets[ref].selector][0]
        if selector_kind not in spec.target_kinds:
            raise DslError(f"{path}.target", f"{kind} can't affect {targets[ref].selector}")
        fields["target"] = ref
    if "amount" in spec.fields:
        fields["amount"] = _grounded(
            _int(data["amount"], 1, MAX_AMOUNT, f"{path}.amount"), grounded, f"{path}.amount"
        )
    if "color" in spec.fields:
        fields["color"] = _enum(data["color"], MANA_COLORS, f"{path}.color")
    if "power" in spec.fields:
        low = 0 if kind == "CREATE_TOKEN" else -MAX_AMOUNT
        for stat in ("power", "toughness"):
            fields[stat] = _grounded(
                _int(data[stat], low, MAX_AMOUNT, f"{path}.{stat}"), grounded, f"{path}.{stat}"
            )
    if "keyword" in spec.fields:
        fields["keyword"] = _enum(data["keyword"], SUPPORTED_KEYWORDS, f"{path}.keyword")
    if "counter" in spec.fields:
        fields["counter"] = _enum(data["counter"], COUNTERS, f"{path}.counter")
    if "duration" in spec.fields:
        fields["duration"] = _enum(data["duration"], DURATIONS, f"{path}.duration")
    if "card_type" in spec.fields:
        fields["card_type"] = _enum(data["card_type"], CARD_TYPES, f"{path}.card_type")
    if "token_name" in spec.fields:
        name = data["token_name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 40:
            raise DslError(f"{path}.token_name", "must be a string of 1 to 40 characters")
        fields["token_name"] = name
    return Op(kind, **fields)
