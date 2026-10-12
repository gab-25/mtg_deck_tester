"""The closed effect DSL: what a compiled card can do, and its validator.

A compiled card is *data*: a list of abilities, each binding a trigger to a
short sequence of operations from a fixed vocabulary. The vocabulary is closed
on purpose — it is what turns the judge's work into multiple-choice questions —
and every op and every selector carries a one-line English description, which
is exactly the option text the judge model sees.

The persisted form is the validated dict (``to_json``), never the dataclasses,
so refactoring a class does not invalidate the cache. :func:`validate_program`
rejects — never coerces — anything outside the vocabulary.

Each op also has a small grammar: the clause shapes that state it. The judge
uses it to read the op's fields off the clause in code (amounts, P/T, the
selector phrase) once the model has named the op, so a field is never a
model's guess and an op whose grammar does not cover the clause is refused.

Pure: no Django, no I/O.
"""

import re
from dataclasses import dataclass, field

from .cardrules import NUMBER_WORDS, number_value
from .manacost import ManaCostError, parse_mana_cost, parse_mana_symbols

# Bump only when the accepted vocabulary or a program's meaning changes: it is
# part of the compiled-rules cache key.
DSL_VERSION = "5"


class DslError(ValueError):
    """A program the kernel must not run: ``path`` points at the offending part."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


# -- triggers ---------------------------------------------------------------


class Trigger:
    SPELL = "SPELL"
    ETB = "ETB"
    DIES = "DIES"
    ATTACKS = "ATTACKS"
    UPKEEP = "UPKEEP"
    END_STEP = "END_STEP"
    ACTIVATED = "ACTIVATED"
    STATIC = "STATIC"
    # Event triggers: something happening anywhere, filtered by ``Ability.when``.
    CAST = "CAST"
    ENTERS = "ENTERS"
    COMBAT_DAMAGE = "COMBAT_DAMAGE"
    YOU_ATTACK = "YOU_ATTACK"
    BEGIN_COMBAT = "BEGIN_COMBAT"
    LEAVES = "LEAVES"
    GAIN_LIFE = "GAIN_LIFE"
    OPPONENT_DRAWS = "OPPONENT_DRAWS"


TRIGGERS = {
    Trigger.SPELL: "the effect of an instant or sorcery as it resolves",
    Trigger.ETB: "when this permanent enters the battlefield",
    Trigger.DIES: "when this creature dies",
    Trigger.ATTACKS: "whenever this creature attacks",
    Trigger.UPKEEP: "at the beginning of its controller's upkeep",
    Trigger.END_STEP: "at the beginning of its controller's end step",
    Trigger.ACTIVATED: "an ability its controller activates by paying a cost",
    Trigger.STATIC: "always true while this permanent is on the battlefield",
    Trigger.CAST: "whenever a player (you, an opponent, anyone) casts a spell of a kind",
    Trigger.ENTERS: "whenever another permanent of a kind enters under its controller's control",
    Trigger.COMBAT_DAMAGE: "whenever this creature deals combat damage to a player",
    Trigger.YOU_ATTACK: "whenever its controller attacks with one or more creatures",
    Trigger.BEGIN_COMBAT: "at the beginning of combat on its controller's turn",
    Trigger.LEAVES: "when this permanent leaves the battlefield, whatever the zone",
    Trigger.GAIN_LIFE: "whenever its controller gains life",
    Trigger.OPPONENT_DRAWS: "whenever an opponent of its controller draws a card",
}

TRIGGERED = (
    Trigger.ETB,
    Trigger.DIES,
    Trigger.ATTACKS,
    Trigger.UPKEEP,
    Trigger.END_STEP,
    Trigger.CAST,
    Trigger.ENTERS,
    Trigger.COMBAT_DAMAGE,
    Trigger.YOU_ATTACK,
    Trigger.BEGIN_COMBAT,
    Trigger.LEAVES,
    Trigger.GAIN_LIFE,
    Trigger.OPPONENT_DRAWS,
)
# What ``Ability.when`` may say for the event triggers that take a filter:
# CAST is "<who>:<spell kind>", ENTERS the kind of permanent.
CAST_WHO = ("YOU", "OPPONENT", "ANY")
CAST_WHAT = ("SPELL", "NONCREATURE", "CREATURE", "INSTANT_OR_SORCERY", "ARTIFACT", "COLORLESS")
ENTERS_WHAT = ("OTHER_CREATURE", "CREATURE", "LAND")
# Triggers whose event has a player "that player" / "they" names.
EVENT_PLAYER_TRIGGERS = (Trigger.COMBAT_DAMAGE, Trigger.OPPONENT_DRAWS, Trigger.CAST)


# -- selectors --------------------------------------------------------------


class SelKind:
    PLAYER = "player"
    CREATURE = "creature"
    PERMANENT = "permanent"
    SPELL = "spell"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    description: str
    kind: str
    # Targeted selectors are chosen on cast and re-checked on resolution.
    targeted: bool
    # How the selector reads in oracle text (lowercase regex, no anchors).
    phrase: str


# "an opponent controls" after a permanent selector: a restriction, not another selector.
_OPPONENTS = r"(?: an opponent controls| you don't control)?"

SELECTORS: dict[str, SelectorSpec] = {
    "ANY_TARGET": SelectorSpec(
        "any target: one creature or one player of the caster's choice", SelKind.ANY, True, r"any target"
    ),
    "TARGET_CREATURE_YOU_CONTROL": SelectorSpec(
        "one target creature its controller controls", SelKind.CREATURE, True, r"target creature you control"
    ),
    "TARGET_CREATURE_OPPONENT_CONTROLS": SelectorSpec(
        "one target creature an opponent controls",
        SelKind.CREATURE,
        True,
        r"target creature (?:an opponent controls|you don't control)",
    ),
    "TARGET_CREATURE_OR_PLANESWALKER": SelectorSpec(
        "one target creature or planeswalker", SelKind.PERMANENT, True, r"target creature or planeswalker"
    ),
    "TARGET_CREATURE": SelectorSpec(
        "one target creature, whoever controls it",
        SelKind.CREATURE,
        True,
        r"target (?:tapped |legendary )?creature(?: with (?:power|mana value) \w+ or (?:less|greater))?",
    ),
    "TARGET_ARTIFACT_OR_ENCHANTMENT": SelectorSpec(
        "one target artifact or enchantment", SelKind.PERMANENT, True, rf"target artifact or enchantment{_OPPONENTS}"
    ),
    "TARGET_ARTIFACT": SelectorSpec("one target artifact", SelKind.PERMANENT, True, rf"target artifact{_OPPONENTS}"),
    "TARGET_ENCHANTMENT": SelectorSpec("one target enchantment", SelKind.PERMANENT, True, rf"target enchantment{_OPPONENTS}"),
    "TARGET_NONLAND_PERMANENT": SelectorSpec(
        "one target permanent that is not a land", SelKind.PERMANENT, True, rf"target nonland permanent{_OPPONENTS}"
    ),
    "TARGET_NONBASIC_LAND": SelectorSpec(
        "one target land that is not a basic land", SelKind.PERMANENT, True, rf"target nonbasic land{_OPPONENTS}"
    ),
    "TARGET_LAND": SelectorSpec("one target land", SelKind.PERMANENT, True, rf"target land{_OPPONENTS}"),
    "TARGET_PERMANENT": SelectorSpec("one target permanent of any type", SelKind.PERMANENT, True, rf"target permanent{_OPPONENTS}"),
    "TARGET_NONCREATURE_SPELL": SelectorSpec(
        "one target spell on the stack that is not a creature spell",
        SelKind.SPELL,
        True,
        r"target noncreature spell",
    ),
    "TARGET_CREATURE_SPELL": SelectorSpec(
        "one target creature spell on the stack", SelKind.SPELL, True, r"target creature spell"
    ),
    "TARGET_INSTANT_OR_SORCERY_SPELL": SelectorSpec(
        "one target instant or sorcery spell on the stack", SelKind.SPELL, True, r"target instant or sorcery spell"
    ),
    "TARGET_SPELL": SelectorSpec(
        "one target spell on the stack", SelKind.SPELL, True, r"target spell(?: with mana value \w+)?"
    ),
    "TARGET_OPPONENT": SelectorSpec("one target opponent", SelKind.PLAYER, True, r"target opponent"),
    "TARGET_PLAYER": SelectorSpec("one target player, possibly yourself", SelKind.PLAYER, True, r"target player"),
    "EACH_OPPONENT": SelectorSpec("every opponent still in the game", SelKind.PLAYER, False, r"each opponent"),
    "EACH_PLAYER": SelectorSpec("every player still in the game", SelKind.PLAYER, False, r"each player"),
    "CONTROLLER_OF_IT": SelectorSpec(
        "the controller of the creature, permanent or spell an earlier part of this ability affected",
        SelKind.PLAYER,
        False,
        r"its controller",
    ),
    "THAT_PLAYER": SelectorSpec(
        "the player the triggering event was about (who cast, drew, or was dealt damage)",
        SelKind.PLAYER,
        False,
        r"that player|they",
    ),
    "YOU": SelectorSpec("the player who controls this card", SelKind.PLAYER, False, r"you"),
    "SELF": SelectorSpec(
        "this permanent itself",
        SelKind.CREATURE,
        False,
        r"this (?:creature|artifact|enchantment|land|permanent)",
    ),
    "EACH_OTHER_CREATURE_YOU_CONTROL": SelectorSpec(
        "every other creature this card's controller controls",
        SelKind.CREATURE,
        False,
        r"(?:each )?other creatures? you control",
    ),
    "EACH_CREATURE_YOU_CONTROL": SelectorSpec(
        "every creature this card's controller controls",
        SelKind.CREATURE,
        False,
        r"(?:each creature you control|creatures you control)",
    ),
    "EACH_CREATURE_OPPONENTS_CONTROL": SelectorSpec(
        "every creature the opponents control",
        SelKind.CREATURE,
        False,
        r"(?:each creature your opponents control|creatures your opponents control)",
    ),
    "EACH_OTHER_CREATURE": SelectorSpec(
        "every creature except this one", SelKind.CREATURE, False, r"(?:each|all) other creatures?"
    ),
    "EACH_CREATURE": SelectorSpec("every creature on the battlefield", SelKind.CREATURE, False, r"(?:each creature|all creatures)"),
}

# The option the judge picks when no selector fits the clause.
NONE_OPTION = "NONE"

PLAYER_KINDS = {SelKind.PLAYER}
# Selectors that may carry a restriction, and the restrictions they accept.
RESTRICTIONS = {
    "POWER_LE": "power less than or equal to the value",
    "POWER_GE": "power greater than or equal to the value",
    "MV_LE": "mana value less than or equal to the value",
    "MV_EQ": "mana value equal to the value",
    "TAPPED": "the object is tapped",
    "LEGENDARY": "the object is legendary",
    "OPPONENT": "an opponent of its controller controls the object",
}
# Restrictions that take no value.
FLAG_RESTRICTIONS = ("TAPPED", "LEGENDARY", "OPPONENT")
_OPPONENT_ONLY = frozenset({"OPPONENT"})
_RESTRICTABLE = {
    "TARGET_CREATURE": frozenset({"POWER_LE", "POWER_GE", "MV_LE", "TAPPED", "LEGENDARY"}),
    "TARGET_SPELL": frozenset({"MV_EQ"}),
    "TARGET_ARTIFACT_OR_ENCHANTMENT": _OPPONENT_ONLY,
    "TARGET_ARTIFACT": _OPPONENT_ONLY,
    "TARGET_ENCHANTMENT": _OPPONENT_ONLY,
    "TARGET_NONLAND_PERMANENT": _OPPONENT_ONLY,
    "TARGET_NONBASIC_LAND": _OPPONENT_ONLY,
    "TARGET_LAND": _OPPONENT_ONLY,
    "TARGET_PERMANENT": _OPPONENT_ONLY,
}
# Selectors naming an object through an earlier target of the same ability.
DERIVED_SELECTORS = ("CONTROLLER_OF_IT",)


def selector_phrase_regex() -> str:
    """One alternation matching any selector phrase, most specific first."""
    return "|".join(f"(?:{spec.phrase})" for spec in SELECTORS.values())


def resolve_phrase(phrase: str) -> tuple[str, dict | None] | None:
    """Maps a selector phrase found in a clause to ``(selector, restriction)``."""
    phrase = phrase.strip()
    for name, spec in SELECTORS.items():
        if re.fullmatch(spec.phrase, phrase):
            return name, _restriction(name, phrase)
    return None


def _restriction(selector: str, phrase: str) -> dict | None:
    if selector not in _RESTRICTABLE:
        return None
    if phrase.startswith("target tapped "):
        return {"kind": "TAPPED"}
    if phrase.startswith("target legendary "):
        return {"kind": "LEGENDARY"}
    if phrase.endswith((" an opponent controls", " you don't control")):
        return {"kind": "OPPONENT"}
    exact = re.search(r"with mana value (\w+)$", phrase)
    if exact:
        return {"kind": "MV_EQ", "value": number_value(exact.group(1))}
    match = re.search(r"with (power|mana value) (\w+) or (less|greater)", phrase)
    if not match:
        return None
    value = number_value(match.group(2))
    if match.group(1) == "power":
        kind = "POWER_LE" if match.group(3) == "less" else "POWER_GE"
    elif match.group(3) == "less":
        kind = "MV_LE"
    else:
        return {"kind": "UNSUPPORTED"}
    return {"kind": kind, "value": value}


# -- operations ---------------------------------------------------------------

KEYWORD_WORDS = (
    "double strike",
    "first strike",
    "flying",
    "trample",
    "haste",
    "vigilance",
    "lifelink",
    "deathtouch",
    "menace",
    "reach",
    "defender",
    "hexproof",
    "indestructible",
)
GRANTABLE_KEYWORDS = KEYWORD_WORDS
COUNTERS = ("+1/+1", "-1/-1")
SACRIFICE_WHAT = ("creature", "permanent", "artifact", "enchantment", "land", "nonland permanent")
TOKEN_COLORS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}

_NUMBER_WORD = r"\d+|x|a|an|one|two|three|four|five|six|seven|eight|nine|ten"
_N = rf"(?P<n>{_NUMBER_WORD})"
# A second number in one clause ("look at the top five … put two of them").
_K = rf"(?P<k>{_NUMBER_WORD})"
_T = "(?P<t>" + selector_phrase_regex() + ")"
_KW = "(?:" + "|".join(KEYWORD_WORDS) + ")"
# "it deals" is read as this permanent only when nothing earlier in the ability
# was targeted (the judge checks): otherwise "it" is that target.
_SELF_SUBJECT = r"(?:(?:this (?:creature|spell|artifact|enchantment|land|permanent)|it) )?"
# "equal to its power": the last known value of the object an earlier part affected.
_ITS_STAT = r"equal to its (?P<stat>power|toughness|mana value)"
_COLOR_WORDS = r"(?:white|blue|black|red|green|colorless)(?: and (?:white|blue|black|red|green))?"


@dataclass(frozen=True, slots=True)
class OpSpec:
    description: str
    # Fields the op requires, beyond "op".
    required: tuple[str, ...]
    optional: tuple[str, ...]
    # Selector kinds its target may be (empty: the op takes no target).
    target_kinds: frozenset
    # Clause shapes stating it (lowercase, matched against the whole clause).
    grammar: tuple[str, ...] = field(default=())


_PLAYERS = frozenset({SelKind.PLAYER})
_CREATURES = frozenset({SelKind.CREATURE})
_PERMANENTS = frozenset({SelKind.CREATURE, SelKind.PERMANENT})

OPS: dict[str, OpSpec] = {
    "DEAL_DAMAGE": OpSpec(
        "deals a number of damage to a creature or player",
        ("target", "amount"),
        (),
        frozenset({SelKind.ANY, SelKind.PLAYER, SelKind.CREATURE}),
        (rf"^{_SELF_SUBJECT}deals? {_N} damage to {_T}$",),
    ),
    "GAIN_LIFE": OpSpec(
        "a player gains a number of life",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^{_T} gains? {_N} life$", rf"^gain {_N} life$", rf"^{_T} gains? life {_ITS_STAT}$", rf"^gain life {_ITS_STAT}$"),
    ),
    "LOSE_LIFE": OpSpec(
        "a player loses a number of life",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^{_T} loses? {_N} life$", rf"^{_T} loses? life {_ITS_STAT}$"),
    ),
    "DRAW_CARDS": OpSpec(
        "a player draws a number of cards",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^{_T} draws? {_N} cards?$", rf"^draw {_N} cards?$"),
    ),
    "DISCARD_CARDS": OpSpec(
        "a player discards a number of cards from their hand",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^{_T} discards? {_N} cards?$", rf"^discard {_N} cards?$"),
    ),
    "DESTROY": OpSpec(
        "destroys a permanent, putting it into its owner's graveyard",
        ("target",),
        (),
        _PERMANENTS,
        (rf"^destroy {_T}$",),
    ),
    "EXILE": OpSpec(
        "exiles a permanent, removing it from the game",
        ("target",),
        (),
        _PERMANENTS,
        (rf"^exile {_T}$",),
    ),
    "RETURN_TO_HAND": OpSpec(
        "returns a permanent from the battlefield to its owner's hand",
        ("target",),
        (),
        _PERMANENTS,
        (rf"^return {_T} to (?:its|their) owners?'?s?'? hands?$",),
    ),
    "TAP": OpSpec("taps a permanent", ("target",), (), _PERMANENTS, (rf"^tap {_T}$",)),
    "UNTAP": OpSpec("untaps a permanent", ("target",), (), _PERMANENTS, (rf"^untap {_T}$",)),
    "ADD_MANA": OpSpec(
        "adds mana to its controller's mana pool",
        (),
        ("mana", "any_color"),
        frozenset(),
        (r"^add (?P<mana>(?:\{[wubrgc]\})+)$", rf"^add {_N} mana of any(?: one)? color$"),
    ),
    "CREATE_TOKEN": OpSpec(
        "creates creature tokens with a printed power and toughness under its controller's control",
        ("amount", "power", "toughness", "colors", "subtype"),
        ("keywords",),
        frozenset(),
        (
            rf"^create {_N} (?P<p>\d+)/(?P<q>\d+) (?P<colors>{_COLOR_WORDS}) (?P<sub>[a-z]+(?: [a-z]+)?) "
            rf"(?:artifact )?creature tokens?(?: with (?P<kw>{_KW}(?:,? (?:and )?{_KW})*))?$",
        ),
    ),
    "MODIFY_PT": OpSpec(
        "changes the power and toughness of creatures, until end of turn or for as long as the source stays",
        ("target", "power", "toughness", "duration"),
        (),
        _CREATURES,
        (rf"^{_T} gets? (?P<p>[+-]\d+)/(?P<q>[+-]\d+)(?P<eot> until end of turn)?$",),
    ),
    "GRANT_KEYWORD": OpSpec(
        "gives creatures a keyword ability such as flying or trample",
        ("target", "keyword", "duration"),
        (),
        _CREATURES,
        (rf"^{_T} (?:gains?|has|have) (?P<kw>{_KW})(?P<eot> until end of turn)?$",),
    ),
    "PUT_COUNTERS": OpSpec(
        "puts +1/+1 or -1/-1 counters on creatures",
        ("target", "amount", "counter"),
        (),
        _CREATURES,
        (rf"^put {_N} (?P<c>\+1/\+1|-1/-1) counters? on {_T}$",),
    ),
    "SACRIFICE": OpSpec(
        "a permanent is sacrificed: this one, or ones its controller picks when a player is told to sacrifice",
        ("target",),
        ("amount", "what"),
        frozenset({SelKind.PLAYER, SelKind.CREATURE}),
        (
            rf"^sacrifice {_T}$",
            rf"^{_T} sacrifices? {_N} (?P<what>nonland permanent|creature|permanent|artifact|enchantment|land)s?$",
        ),
    ),
    "DISCARD_CHOSEN": OpSpec(
        "a player reveals their hand and discards a card of a kind that this card's controller picks from it",
        ("target", "filter"),
        ("max_mv",),
        _PLAYERS,
        (
            rf"^{_T} reveals (?:their|his or her) hand\. you choose an? (?P<filter>[a-z, ]+?) card from it"
            r"(?: with mana value (?P<mv>\d+) or less)?\. that player discards that card$",
        ),
    ),
    "LOOK_AT_HAND": OpSpec(
        "this card's controller looks at a player's hand",
        ("target",),
        (),
        _PLAYERS,
        (rf"^look at {_T}'s hand$",),
    ),
    "MILL": OpSpec(
        "a player puts the top cards of their library into their graveyard",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^{_T} mills? {_N} cards?$", rf"^mill {_N} cards?$"),
    ),
    "SEARCH_LIBRARY": OpSpec(
        "its controller searches their library for cards of a kind and puts them in a zone, then shuffles",
        ("target", "amount", "filter", "to"),
        (),
        _PLAYERS,
        (
            rf"^search your library for {_N} (?:(?P<filter>[a-z ,]+?) )?cards?, put (?:it|that card|them|those cards) "
            r"(?P<to>onto the battlefield(?: tapped)?|into your hand|on top of your library|into your graveyard)$",
        ),
    ),
    "DIG": OpSpec(
        "its controller looks at the top cards of their library, puts some of them into their hand "
        "and the rest on the bottom or into their graveyard",
        ("target", "amount", "count", "to"),
        (),
        _PLAYERS,
        (
            rf"^look at the top {_N} cards of your library\. put {_K} of them into your hand and the (?:rest|other) "
            r"(?P<rest>on the bottom of your library(?: in any order)?|into your graveyard)$",
        ),
    ),
    "PUT_BACK": OpSpec(
        "its controller puts cards from their hand on top of their library",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^put {_N} cards? from your hand on top of your library(?: in any order)?$",),
    ),
    "ARRANGE_TOP": OpSpec(
        "its controller looks at the top cards of their library and puts them back in any order, "
        "possibly allowed to shuffle instead",
        ("target", "amount"),
        ("shuffle",),
        _PLAYERS,
        (
            rf"^look at the top {_N} cards of your library, then put them back in any order"
            r"(?P<shuffle>\. you may shuffle)?$",
        ),
    ),
    "SCRY": OpSpec(
        "a player looks at the top cards of their library and puts each on the top or the bottom",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^scry {_N}$",),
    ),
    "SURVEIL": OpSpec(
        "a player looks at the top cards of their library and puts each on the top or into their graveyard",
        ("target", "amount"),
        (),
        _PLAYERS,
        (rf"^surveil {_N}$",),
    ),
    "COUNTER_SPELL": OpSpec(
        "counters a spell on the stack, which goes to its owner's graveyard without effect",
        ("target",),
        (),
        frozenset({SelKind.SPELL}),
        (rf"^counter {_T}$",),
    ),
    "COUNTER_UNLESS_PAYS": OpSpec(
        "counters a spell on the stack unless its controller pays an amount of generic mana",
        ("target", "mana"),
        (),
        frozenset({SelKind.SPELL}),
        (rf"^counter {_T} unless its controller pays (?P<mana>\{{\d+\}})$",),
    ),
    "CREATE_ARTIFACT_TOKEN": OpSpec(
        "a player creates Treasure, Clue, Food or Blood artifact tokens (investigate creates a Clue)",
        ("target", "amount", "token"),
        (),
        _PLAYERS,
        (rf"^(?:{_T} creates?|create) {_N} (?P<token>treasure|clue|food|blood) tokens?$", r"^investigate$"),
    ),
}

NONE_OP_DESCRIPTION = (
    "the clause does something not in this list, or more than one of these things, "
    "or depends on a condition or a choice"
)
NONE_SELECTOR_DESCRIPTION = "the clause affects something not in this list, or several of these"

# Ops a static ability may carry, and ops that only make sense once.
STATIC_OPS = {"MODIFY_PT", "GRANT_KEYWORD"}
# Amounts computed from the last known information of an earlier target.
EXPRESSIONS = {"power": "POWER_OF_IT", "toughness": "TOUGHNESS_OF_IT", "mana value": "MV_OF_IT"}
ARTIFACT_TOKENS = ("TREASURE", "CLUE", "FOOD", "BLOOD")
DIG_DESTINATIONS = {"on the bottom of your library": "BOTTOM", "into your graveyard": "GRAVEYARD"}
DURATIONS = ("EOT", "STATIC")

AMOUNT_MAX = 20
PT_MAX = 20
MAX_ABILITIES = 12
MAX_OPS = 6
MAX_DISCARD_COST = 3


BASIC_LAND_TYPES = ("plains", "island", "swamp", "mountain", "forest")
SEARCH_FILTERS = (
    "CARD",
    "LAND",
    "BASIC_LAND",
    "CREATURE",
    "ARTIFACT",
    "ENCHANTMENT",
    "INSTANT",
    "SORCERY",
    "INSTANT_OR_SORCERY",
    "NONLAND",
    "NONCREATURE_NONLAND",
    "ARTIFACT_OR_ENCHANTMENT",
)
SEARCH_DESTINATIONS = {
    "onto the battlefield": "BATTLEFIELD",
    "onto the battlefield tapped": "BATTLEFIELD_TAPPED",
    "into your hand": "HAND",
    "on top of your library": "TOP",
    "into your graveyard": "GRAVEYARD",
}


def search_filter(phrase: str) -> str | None:
    """The filter code of a library-search phrase (``"forest or island"`` -> ``TYPES:forest|island``)."""
    phrase = phrase.strip()
    simple = {
        "": "CARD",
        "land": "LAND",
        "basic land": "BASIC_LAND",
        "creature": "CREATURE",
        "artifact": "ARTIFACT",
        "enchantment": "ENCHANTMENT",
        "instant": "INSTANT",
        "sorcery": "SORCERY",
        "instant or sorcery": "INSTANT_OR_SORCERY",
        "nonland": "NONLAND",
        "noncreature, nonland": "NONCREATURE_NONLAND",
        "artifact or enchantment": "ARTIFACT_OR_ENCHANTMENT",
    }
    if phrase in simple:
        return simple[phrase]
    words = [w for w in re.split(r",? or |, ", phrase) if w]
    if words and all(w in BASIC_LAND_TYPES for w in words):
        return "TYPES:" + "|".join(words)
    return None


def _amount_word(word: str) -> int | str:
    return "X" if word == "x" else 1 if word in ("a", "an") else number_value(word)


def parse_clause(op: str, clause: str) -> dict | None:
    """Reads ``op``'s fields off ``clause`` with the op's grammar.

    Returns the raw fields (``selector``, ``restriction``, ``amount``…) or None
    when no grammar of ``op`` covers the whole clause.
    """
    spec = OPS.get(op)
    if spec is None:
        return None
    for pattern in spec.grammar:
        match = re.fullmatch(pattern, clause)
        if not match:
            continue
        groups = {k: v for k, v in match.groupdict().items() if v is not None}
        fields: dict = {}
        if "t" in groups:
            resolved = resolve_phrase(groups["t"])
            if resolved is None:
                return None
            fields["selector"], fields["restriction"] = resolved
        elif spec.target_kinds:
            # Imperative clauses ("draw two cards") are about their controller.
            fields["selector"], fields["restriction"] = "YOU", None
        if "n" in groups:
            fields["amount"] = _amount_word(groups["n"])
        if "k" in groups:
            fields["count"] = _amount_word(groups["k"])
        if "stat" in groups:
            fields["amount"] = EXPRESSIONS[groups["stat"]]
        if "p" in groups:
            fields["power"], fields["toughness"] = int(groups["p"]), int(groups["q"])
        if "kw" in groups:
            if op == "CREATE_TOKEN":
                fields["keywords"] = sorted(set(re.findall(_KW, groups["kw"])), key=KEYWORD_WORDS.index)
            else:
                fields["keyword"] = groups["kw"]
        if op in ("MODIFY_PT", "GRANT_KEYWORD"):
            fields["duration"] = "EOT" if "eot" in groups else "STATIC"
        if "c" in groups:
            fields["counter"] = groups["c"]
        if op in ("SEARCH_LIBRARY", "DISCARD_CHOSEN"):
            fields["filter"] = search_filter(groups.get("filter", ""))
            if fields["filter"] is None:
                return None
        if op == "SEARCH_LIBRARY":
            fields["to"] = SEARCH_DESTINATIONS[groups["to"]]
        if "mv" in groups:
            fields["max_mv"] = int(groups["mv"])
        if op == "DIG":
            fields["to"] = DIG_DESTINATIONS[groups["rest"].removesuffix(" in any order")]
        if "shuffle" in groups:
            fields["shuffle"] = True
        if op == "CREATE_ARTIFACT_TOKEN":
            fields["token"] = groups["token"].upper() if "token" in groups else "CLUE"
            fields.setdefault("amount", 1)
        if "what" in groups:
            fields["what"] = groups["what"]
        if "mana" in groups:
            fields["mana"] = groups["mana"].upper()
        elif op == "ADD_MANA":
            fields["any_color"] = fields.pop("amount")
        if "colors" in groups:
            fields["colors"] = [TOKEN_COLORS[c] for c in re.findall(r"[a-z]+", groups["colors"]) if c in TOKEN_COLORS]
            fields["subtype"] = groups["sub"]
        return fields
    return None


# -- the program --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    key: str
    selector: str
    restriction: str = ""
    restriction_value: int | None = None
    # A derived selector (CONTROLLER_OF_IT): the key of the earlier target it follows.
    of: str = ""

    @property
    def spec(self) -> SelectorSpec:
        return SELECTORS[self.selector]

    @property
    def targeted(self) -> bool:
        return self.spec.targeted


@dataclass(frozen=True, slots=True)
class Op:
    op: str
    target: str = ""
    amount: int | str | None = None
    power: int | None = None
    toughness: int | None = None
    duration: str = ""
    keyword: str = ""
    counter: str = ""
    mana: str = ""
    any_color: int | None = None
    colors: tuple[str, ...] = ()
    subtype: str = ""
    keywords: tuple[str, ...] = ()
    what: str = ""
    # SEARCH_LIBRARY / DISCARD_CHOSEN: which cards qualify; SEARCH_LIBRARY / DIG:
    # where the found card (or the rest) goes.
    filter: str = ""
    to: str = ""
    max_mv: int | None = None
    # DIG: how many of the cards looked at go to the hand.
    count: int | None = None
    # ARRANGE_TOP: its controller may shuffle instead.
    shuffle: bool = False
    # CREATE_ARTIFACT_TOKEN: which predefined token.
    token: str = ""
    # A computed amount (POWER_OF_IT…): the key of the earlier target it reads.
    of: str = ""


@dataclass(frozen=True, slots=True)
class Cost:
    mana: str = ""
    tap: bool = False
    sacrifice_self: bool = False
    life: int = 0
    # Cards discarded from hand (the least valuable, like mana, paid automatically).
    discard: int = 0

    @property
    def empty(self) -> bool:
        return not (self.mana or self.tap or self.sacrifice_self or self.life or self.discard)


@dataclass(frozen=True, slots=True)
class Ability:
    trigger: str
    ops: tuple[Op, ...]
    targets: tuple[Target, ...] = ()
    cost: Cost | None = None
    mode_group: int | None = None
    text: str = ""
    # The event filter of CAST and ENTERS triggers (see CAST_WHO, ENTERS_WHAT).
    when: str = ""

    def target(self, key: str) -> Target | None:
        return next((t for t in self.targets if t.key == key), None)

    @property
    def is_mana_ability(self) -> bool:
        return self.trigger == Trigger.ACTIVATED and any(op.op == "ADD_MANA" for op in self.ops)


@dataclass(frozen=True, slots=True)
class CardProgram:
    abilities: tuple[Ability, ...] = ()
    version: str = DSL_VERSION

    def by_trigger(self, trigger: str) -> list[Ability]:
        return [a for a in self.abilities if a.trigger == trigger]

    @property
    def modes(self) -> list[Ability]:
        return [a for a in self.abilities if a.mode_group is not None]


_PROGRAM_KEYS = {"version", "abilities"}
_ABILITY_KEYS = {"trigger", "ops", "targets", "cost", "mode_group", "text", "when"}
_TARGET_KEYS = {"selector", "restriction", "of"}
_RESTRICTION_KEYS = {"kind", "value"}
_COST_KEYS = {"mana", "tap", "sacrifice_self", "life", "discard"}
_OP_FIELDS = {
    "op",
    "target",
    "amount",
    "power",
    "toughness",
    "duration",
    "keyword",
    "counter",
    "mana",
    "any_color",
    "colors",
    "subtype",
    "keywords",
    "what",
    "filter",
    "to",
    "max_mv",
    "count",
    "shuffle",
    "token",
    "of",
}


def _grounded_numbers(text: str) -> set[int]:
    numbers = {int(n) for n in re.findall(r"\d+", text)}
    numbers |= {value for word, value in NUMBER_WORDS.items() if re.search(rf"\b{word}\b", text)}
    if re.search(r"\b(?:a|an|investigate)\b", text):
        numbers.add(1)
    return numbers


def _check_keys(data, allowed: set, path: str) -> None:
    if not isinstance(data, dict):
        raise DslError(path, "must be an object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise DslError(path, f"unknown key {unknown[0]!r}")


def _int(value, path: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DslError(path, "must be an integer")
    if not low <= value <= high:
        raise DslError(path, f"must be between {low} and {high}")
    return value


def validate_program(payload, facts) -> CardProgram:
    """Checks ``payload`` against the DSL and ``facts``; returns the program.

    ``facts`` is the card's :class:`~.cardrules.CardFacts`: numbers are
    grounded in its residual text and ``{X}`` amounts in its mana cost.
    Raises :class:`DslError` on the first problem found.
    """
    _check_keys(payload, _PROGRAM_KEYS, "program")
    if payload.get("version") != DSL_VERSION:
        raise DslError("program.version", f"must be {DSL_VERSION!r}")
    abilities = payload.get("abilities")
    if not isinstance(abilities, list):
        raise DslError("program.abilities", "must be a list")
    if len(abilities) > MAX_ABILITIES:
        raise DslError("program.abilities", f"at most {MAX_ABILITIES} abilities")

    grounded = _grounded_numbers(facts.residual)
    has_x = "{X}" in (facts.mana_cost or "").upper() and re.search(r"\bx\b", facts.residual)
    result = []
    for index, ability in enumerate(abilities):
        result.append(_validate_ability(ability, f"abilities[{index}]", facts, grounded, bool(has_x)))
    if facts.is_instant_or_sorcery and any(a.trigger != Trigger.SPELL for a in result):
        raise DslError("program.abilities", "an instant or sorcery only has spell abilities")
    if facts.is_permanent and any(a.trigger == Trigger.SPELL for a in result):
        raise DslError("program.abilities", "a permanent has no spell ability")
    return CardProgram(tuple(result))


def _validate_ability(data, path: str, facts, grounded: set[int], has_x: bool) -> Ability:
    _check_keys(data, _ABILITY_KEYS, path)
    trigger = data.get("trigger")
    if trigger not in TRIGGERS:
        raise DslError(f"{path}.trigger", f"unknown trigger {trigger!r}")

    cost = None
    if trigger == Trigger.ACTIVATED:
        raw_cost = data.get("cost")
        _check_keys(raw_cost, _COST_KEYS, f"{path}.cost")
        cost = Cost(
            mana=raw_cost.get("mana") or "",
            tap=raw_cost.get("tap") is True,
            sacrifice_self=raw_cost.get("sacrifice_self") is True,
            life=_int(raw_cost.get("life", 0), f"{path}.cost.life", 0, AMOUNT_MAX),
            discard=_int(raw_cost.get("discard", 0), f"{path}.cost.discard", 0, MAX_DISCARD_COST),
        )
        if cost.discard and cost.discard not in grounded:
            raise DslError(f"{path}.cost.discard", f"{cost.discard} does not appear in the card's text")
        if cost.life and cost.life not in grounded:
            raise DslError(f"{path}.cost.life", f"{cost.life} does not appear in the card's text")
        if cost.mana:
            try:
                parsed = parse_mana_cost(cost.mana)
            except ManaCostError as exc:
                raise DslError(f"{path}.cost.mana", str(exc)) from exc
            if parsed.x:
                raise DslError(f"{path}.cost.mana", "{X} in an activation cost is not supported")
        if cost.empty:
            # The compile-time guard against free, endlessly repeatable abilities.
            raise DslError(f"{path}.cost", "an activated ability needs a cost")
    elif data.get("cost") is not None:
        raise DslError(f"{path}.cost", "only activated abilities have a cost")

    when = data.get("when") or ""
    if trigger == Trigger.CAST:
        who, _, what = when.partition(":") if isinstance(when, str) else ("", "", "")
        if who not in CAST_WHO or what not in CAST_WHAT:
            raise DslError(f"{path}.when", f"unknown cast filter {when!r}")
    elif trigger == Trigger.ENTERS:
        if when not in ENTERS_WHAT:
            raise DslError(f"{path}.when", f"unknown enters filter {when!r}")
    elif when:
        raise DslError(f"{path}.when", f"{trigger} takes no filter")

    mode_group = data.get("mode_group")
    if mode_group is not None:
        _int(mode_group, f"{path}.mode_group", 0, MAX_ABILITIES)

    raw_targets = data.get("targets") or {}
    if not isinstance(raw_targets, dict):
        raise DslError(f"{path}.targets", "must be an object")
    targets = []
    for key in sorted(raw_targets):
        targets.append(_validate_target(raw_targets[key], key, f"{path}.targets.{key}", grounded, trigger))
    declared = {t.key: t for t in targets}
    for target in targets:
        if (target.selector in DERIVED_SELECTORS) != bool(target.of):
            raise DslError(f"{path}.targets.{target.key}", "a derived selector, and only one, names the target it follows")
        if target.of:
            _check_referent(declared, target.of, f"{path}.targets.{target.key}.of")

    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise DslError(f"{path}.ops", "must be a non-empty list")
    if len(raw_ops) > MAX_OPS:
        raise DslError(f"{path}.ops", f"at most {MAX_OPS} operations")
    by_key = {t.key: t for t in targets}
    ops = [
        _validate_op(op, f"{path}.ops[{i}]", by_key, trigger, grounded, has_x)
        for i, op in enumerate(raw_ops)
    ]

    if any(t.selector == "THAT_PLAYER" for t in targets) and (
        trigger not in EVENT_PLAYER_TRIGGERS or when.startswith("YOU:")
    ):
        raise DslError(f"{path}.targets", "“that player” needs an event about another player")

    used = {op.target for op in ops if op.target}
    unused = sorted(set(by_key) - used)
    if unused:
        raise DslError(f"{path}.targets.{unused[0]}", "declared but no operation uses it")

    if any(op.op == "ADD_MANA" for op in ops):
        if trigger not in (Trigger.ACTIVATED, Trigger.SPELL):
            raise DslError(f"{path}.ops", "mana is only added by spells and activated abilities")
        if trigger == Trigger.ACTIVATED and any(op.op != "ADD_MANA" for op in ops):
            raise DslError(f"{path}.ops", "a mana ability can only add mana")
    if trigger == Trigger.STATIC:
        for i, op in enumerate(ops):
            if op.op not in STATIC_OPS or op.duration != "STATIC":
                raise DslError(f"{path}.ops[{i}]", "a static ability only modifies P/T or grants keywords")
            if by_key[op.target].targeted:
                raise DslError(f"{path}.ops[{i}].target", "a static ability cannot target")
    else:
        for i, op in enumerate(ops):
            if op.duration == "STATIC":
                raise DslError(f"{path}.ops[{i}].duration", "only static abilities last indefinitely")

    return Ability(
        trigger=trigger,
        ops=tuple(ops),
        targets=tuple(targets),
        cost=cost,
        mode_group=mode_group,
        text=str(data.get("text") or ""),
        when=when,
    )


def _check_referent(targets: dict, key, path: str) -> None:
    """``key`` must name a targeted creature, permanent or spell of the same ability."""
    referent = targets.get(key)
    if referent is None or not referent.targeted or referent.spec.kind == SelKind.PLAYER:
        raise DslError(path, f"{key!r} is not a targeted object of this ability")


def _validate_target(data, key: str, path: str, grounded: set[int], trigger: str) -> Target:
    if not re.fullmatch(r"t\d{1,2}", key):
        raise DslError(path, "target keys are t1, t2, …")
    _check_keys(data, _TARGET_KEYS, path)
    selector = data.get("selector")
    if selector not in SELECTORS:
        raise DslError(f"{path}.selector", f"unknown selector {selector!r}")
    if SELECTORS[selector].kind == SelKind.SPELL and trigger != Trigger.SPELL:
        raise DslError(f"{path}.selector", "only spells can target spells")
    of = data.get("of") or ""
    if not isinstance(of, str):
        raise DslError(f"{path}.of", "must be a target key")
    restriction = data.get("restriction")
    if restriction is None:
        return Target(key, selector, of=of)
    if selector not in _RESTRICTABLE:
        raise DslError(f"{path}.restriction", f"{selector} takes no restriction")
    _check_keys(restriction, _RESTRICTION_KEYS, f"{path}.restriction")
    kind = restriction.get("kind")
    if kind not in RESTRICTIONS:
        raise DslError(f"{path}.restriction.kind", f"unknown restriction {kind!r}")
    if kind not in _RESTRICTABLE[selector]:
        raise DslError(f"{path}.restriction", f"{selector} takes no {kind} restriction")
    value = restriction.get("value")
    if kind in FLAG_RESTRICTIONS:
        if value is not None:
            raise DslError(f"{path}.restriction.value", f"{kind} takes no value")
        return Target(key, selector, kind)

    value = _int(value, f"{path}.restriction.value", 0, PT_MAX)
    if value not in grounded:
        raise DslError(f"{path}.restriction.value", f"{value} does not appear in the card's text")
    return Target(key, selector, kind, value)


def _validate_op(data, path: str, targets: dict, trigger: str, grounded: set[int], has_x: bool) -> Op:
    _check_keys(data, _OP_FIELDS, path)
    name = data.get("op")
    spec = OPS.get(name)
    if spec is None:
        raise DslError(f"{path}.op", f"unknown operation {name!r}")
    allowed = {"op", "of", *spec.required, *spec.optional}
    extra = sorted(k for k in data if k not in allowed)
    if extra:
        raise DslError(f"{path}.{extra[0]}", f"{name} has no field {extra[0]!r}")
    missing = [k for k in spec.required if data.get(k) is None]
    if missing:
        raise DslError(f"{path}.{missing[0]}", f"{name} requires {missing[0]!r}")

    fields: dict = {"op": name}
    if "target" in spec.required:
        key = data["target"]
        if key not in targets:
            raise DslError(f"{path}.target", f"undeclared target {key!r}")
        kind = targets[key].spec.kind
        if kind not in spec.target_kinds:
            raise DslError(f"{path}.target", f"{name} cannot affect {targets[key].selector}")
        if name == "SACRIFICE" and kind == SelKind.CREATURE and targets[key].selector != "SELF":
            raise DslError(f"{path}.target", "only this permanent or a player's choice can be sacrificed")
        fields["target"] = key

    def grounded_int(field_name: str, low: int, high: int, *, signed: bool = False) -> int:
        value = _int(data[field_name], f"{path}.{field_name}", low, high)
        if abs(value) not in grounded and not (signed and value == 0):
            raise DslError(f"{path}.{field_name}", f"{value} does not appear in the card's text")
        return value

    if data.get("amount") is not None:
        if data["amount"] == "X":
            if trigger != Trigger.SPELL:
                # X is chosen when a spell is cast; abilities never see it.
                raise DslError(f"{path}.amount", "X amounts only in spell abilities")
            if not has_x:
                raise DslError(f"{path}.amount", "X amounts need {X} in the mana cost")
            fields["amount"] = "X"
        elif data["amount"] in EXPRESSIONS.values():
            if name not in ("GAIN_LIFE", "LOSE_LIFE"):
                raise DslError(f"{path}.amount", f"{name} takes no computed amount")
            _check_referent(targets, data.get("of"), f"{path}.of")
            fields.update(amount=data["amount"], of=data["of"])
        else:
            fields["amount"] = grounded_int("amount", 1, AMOUNT_MAX)
    if data.get("of") is not None and "of" not in fields:
        raise DslError(f"{path}.of", "only a computed amount reads another target")
    if name == "SACRIFICE":
        is_self = targets[fields["target"]].selector == "SELF"
        if is_self and (data.get("amount") is not None or data.get("what") is not None):
            raise DslError(path, "sacrificing this permanent takes no amount")
        if not is_self:
            if data.get("amount") is None or data.get("what") not in SACRIFICE_WHAT:
                raise DslError(path, "a player sacrifices an amount of a kind of permanent")
            fields["what"] = data["what"]
    for stat in ("power", "toughness"):
        if data.get(stat) is not None:
            signed = name == "MODIFY_PT"
            fields[stat] = grounded_int(stat, -PT_MAX if signed else 0, PT_MAX, signed=signed)
    if name in ("MODIFY_PT", "GRANT_KEYWORD"):
        if data["duration"] not in DURATIONS:
            raise DslError(f"{path}.duration", f"unknown duration {data['duration']!r}")
        fields["duration"] = data["duration"]
    if name == "GRANT_KEYWORD":
        if data["keyword"] not in GRANTABLE_KEYWORDS:
            raise DslError(f"{path}.keyword", f"unknown keyword {data['keyword']!r}")
        fields["keyword"] = data["keyword"]
    if name == "PUT_COUNTERS":
        if data["counter"] not in COUNTERS:
            raise DslError(f"{path}.counter", f"unknown counter {data['counter']!r}")
        fields["counter"] = data["counter"]
    if name == "ADD_MANA":
        has_mana, has_any = data.get("mana") is not None, data.get("any_color") is not None
        if has_mana == has_any:
            raise DslError(path, "ADD_MANA needs exactly one of 'mana' and 'any_color'")
        if has_mana:
            try:
                parse_mana_symbols(data["mana"])
            except ManaCostError as exc:
                raise DslError(f"{path}.mana", str(exc)) from exc
            fields["mana"] = data["mana"].upper()
        else:
            fields["any_color"] = grounded_int("any_color", 1, AMOUNT_MAX)
    if name == "CREATE_TOKEN":
        colors = data["colors"]
        if not isinstance(colors, list) or any(c not in "WUBRG" or not c for c in colors):
            raise DslError(f"{path}.colors", "must be a list of W, U, B, R, G")
        subtype = data["subtype"]
        if not isinstance(subtype, str) or not re.fullmatch(r"[a-z]+(?: [a-z]+)?", subtype):
            raise DslError(f"{path}.subtype", "must be one or two lowercase words")
        keywords = data.get("keywords") or []
        if not isinstance(keywords, list) or any(k not in GRANTABLE_KEYWORDS for k in keywords):
            raise DslError(f"{path}.keywords", "unknown keyword")
        fields.update(colors=tuple(colors), subtype=subtype, keywords=tuple(keywords))
        if fields.get("amount") == "X":
            raise DslError(f"{path}.amount", "token counts must be fixed")
    if name == "SEARCH_LIBRARY":
        if targets[fields["target"]].selector != "YOU":
            raise DslError(f"{path}.target", "a player searches only their own library")
        filt = data["filter"]
        types = filt.removeprefix("TYPES:").split("|") if filt.startswith("TYPES:") else None
        if filt not in SEARCH_FILTERS and not (types and all(t in BASIC_LAND_TYPES for t in types)):
            raise DslError(f"{path}.filter", f"unknown filter {filt!r}")
        if data["to"] not in SEARCH_DESTINATIONS.values():
            raise DslError(f"{path}.to", f"unknown destination {data['to']!r}")
        fields.update(filter=filt, to=data["to"])
    if name in ("SEARCH_LIBRARY", "DIG", "PUT_BACK", "ARRANGE_TOP") and targets[fields["target"]].selector != "YOU":
        raise DslError(f"{path}.target", f"{name} only works on its controller's own cards")
    if name == "DISCARD_CHOSEN":
        if data["filter"] not in SEARCH_FILTERS:
            raise DslError(f"{path}.filter", f"unknown filter {data['filter']!r}")
        fields["filter"] = data["filter"]
        if data.get("max_mv") is not None:
            fields["max_mv"] = grounded_int("max_mv", 0, PT_MAX)
    if name == "DIG":
        fields["count"] = grounded_int("count", 1, AMOUNT_MAX)
        if fields.get("amount") == "X" or fields["count"] >= fields["amount"]:
            raise DslError(f"{path}.count", "DIG keeps fewer cards than it looks at")
        if data["to"] not in DIG_DESTINATIONS.values():
            raise DslError(f"{path}.to", f"unknown destination {data['to']!r}")
        fields["to"] = data["to"]
    if name == "ARRANGE_TOP" and data.get("shuffle") is not None:
        if not isinstance(data["shuffle"], bool):
            raise DslError(f"{path}.shuffle", "must be true or false")
        fields["shuffle"] = data["shuffle"]
    if name == "COUNTER_UNLESS_PAYS":
        mana = data["mana"]
        match = re.fullmatch(r"\{(\d+)\}", mana) if isinstance(mana, str) else None
        if match is None:
            raise DslError(f"{path}.mana", "must be one generic amount, like {2}")
        if int(match.group(1)) not in grounded:
            raise DslError(f"{path}.mana", f"{match.group(1)} does not appear in the card's text")
        fields["mana"] = mana
    if name == "CREATE_ARTIFACT_TOKEN":
        if data["token"] not in ARTIFACT_TOKENS:
            raise DslError(f"{path}.token", f"unknown token {data['token']!r}")
        if fields.get("amount") == "X":
            raise DslError(f"{path}.amount", "token counts must be fixed")
        fields["token"] = data["token"]
    return Op(**fields)


# -- (de)serialization -----------------------------------------------------------


def to_json(program: CardProgram) -> dict:
    """The persisted form of a program: the dict :func:`validate_program` accepts."""
    abilities = []
    for ability in program.abilities:
        data: dict = {"trigger": ability.trigger, "ops": [], "text": ability.text}
        if ability.targets:
            data["targets"] = {}
            for target in ability.targets:
                entry: dict = {"selector": target.selector}
                if target.of:
                    entry["of"] = target.of
                if target.restriction:
                    entry["restriction"] = {"kind": target.restriction}
                    if target.restriction_value is not None:
                        entry["restriction"]["value"] = target.restriction_value
                data["targets"][target.key] = entry
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
        for op in ability.ops:
            entry = {"op": op.op}
            for name in _OP_FIELDS - {"op"}:
                value = getattr(op, name)
                if value in ("", None, ()) or value is False:
                    continue
                entry[name] = list(value) if isinstance(value, tuple) else value
            data["ops"].append(entry)
        abilities.append(data)
    return {"version": program.version, "abilities": abilities}


def from_json(payload, facts) -> CardProgram:
    """Reads a persisted program back, validating it again."""
    return validate_program(payload, facts)


# -- rendering -------------------------------------------------------------------

_TRIGGER_TEXT = {
    Trigger.SPELL: "",
    Trigger.ETB: "When it enters: ",
    Trigger.DIES: "When it dies: ",
    Trigger.ATTACKS: "When it attacks: ",
    Trigger.UPKEEP: "At your upkeep: ",
    Trigger.END_STEP: "At your end step: ",
    Trigger.STATIC: "",
    Trigger.ENTERS: "When {when} you control enters: ",
    Trigger.COMBAT_DAMAGE: "When it deals combat damage to a player: ",
    Trigger.YOU_ATTACK: "When you attack: ",
    Trigger.BEGIN_COMBAT: "At the beginning of combat on your turn: ",
    Trigger.LEAVES: "When it leaves the battlefield: ",
    Trigger.GAIN_LIFE: "When you gain life: ",
    Trigger.OPPONENT_DRAWS: "When an opponent draws a card: ",
}


def _selector_text(target: Target | None) -> str:
    if target is None:
        return ""
    text = target.selector.lower().replace("_", " ")
    if target.restriction in FLAG_RESTRICTIONS:
        text += f" ({target.restriction.lower()})"
    elif target.restriction:
        text += f" ({target.restriction.lower().replace('_', ' ')} {target.restriction_value})"
    return text


def describe_op(op: Op, ability: Ability) -> str:
    """A short rendering of one op, e.g. ``deal 3 damage to any target``."""
    who = _selector_text(ability.target(op.target))
    match op.op:
        case "DEAL_DAMAGE":
            return f"deal {op.amount} damage to {who}"
        case "GAIN_LIFE" | "LOSE_LIFE":
            amount = op.amount if isinstance(op.amount, int) else op.amount.removesuffix("_OF_IT").lower() + " of it"
            return f"{who}: {op.op.split('_')[0].lower()} {amount} life"
        case "DRAW_CARDS" | "DISCARD_CARDS" | "MILL":
            verb = {"DRAW_CARDS": "draw", "DISCARD_CARDS": "discard", "MILL": "mill"}[op.op]
            return f"{who}: {verb} {op.amount}"
        case "ADD_MANA":
            return f"add {op.mana}" if op.mana else f"add {op.any_color} mana of any color"
        case "CREATE_TOKEN":
            extra = f" with {', '.join(op.keywords)}" if op.keywords else ""
            return f"create {op.amount} {op.power}/{op.toughness} {op.subtype} token(s){extra}"
        case "MODIFY_PT":
            span = " until end of turn" if op.duration == "EOT" else ""
            return f"{who} get {op.power:+d}/{op.toughness:+d}{span}"
        case "GRANT_KEYWORD":
            span = " until end of turn" if op.duration == "EOT" else ""
            return f"{who} gain {op.keyword}{span}"
        case "PUT_COUNTERS":
            return f"put {op.amount} {op.counter} counter(s) on {who}"
        case "SACRIFICE":
            if op.what:
                return f"{who}: sacrifice {op.amount} {op.what}"
            return f"sacrifice {who}"
        case "COUNTER_SPELL":
            return f"counter {who}"
        case "SEARCH_LIBRARY":
            what = op.filter.removeprefix("TYPES:").replace("|", " or ").replace("_", " ").lower()
            where = op.to.replace("_", " ").lower()
            return f"search your library for {op.amount} {what} card(s), put onto {where}, shuffle"
        case "SCRY" | "SURVEIL":
            return f"{op.op.lower()} {op.amount}"
        case "DISCARD_CHOSEN":
            what = op.filter.replace("_", " ").lower() + (f" (mana value {op.max_mv} or less)" if op.max_mv is not None else "")
            return f"{who}: reveal hand, discard a {what} card you choose"
        case "LOOK_AT_HAND":
            return f"look at {who}'s hand"
        case "DIG":
            rest = "on the bottom" if op.to == "BOTTOM" else "into the graveyard"
            return f"look at the top {op.amount}, put {op.count} into your hand, the rest {rest}"
        case "PUT_BACK":
            return f"put {op.amount} card(s) from your hand on top of your library"
        case "ARRANGE_TOP":
            return f"look at the top {op.amount} and put them back in any order" + (" (or shuffle)" if op.shuffle else "")
        case "COUNTER_UNLESS_PAYS":
            return f"counter {who} unless its controller pays {op.mana}"
        case "CREATE_ARTIFACT_TOKEN":
            return f"{who}: create {op.amount} {op.token.title()} token(s)"
        case "RETURN_TO_HAND":
            return f"return {who} to its owner's hand"
        case _:
            return f"{op.op.lower().replace('_', ' ')} {who}"


def describe_ability(ability: Ability) -> str:
    """A short rendering of one ability for the player view and the log."""
    body = ", then ".join(describe_op(op, ability) for op in ability.ops)
    if ability.trigger == Trigger.ACTIVATED and ability.cost is not None:
        parts = [ability.cost.mana] if ability.cost.mana else []
        if ability.cost.tap:
            parts.append("{T}")
        if ability.cost.life:
            parts.append(f"pay {ability.cost.life} life")
        if ability.cost.discard:
            parts.append(f"discard {ability.cost.discard}")
        if ability.cost.sacrifice_self:
            parts.append("sacrifice it")
        return f"{', '.join(parts)}: {body}"
    if ability.trigger == Trigger.CAST:
        who, _, what = ability.when.partition(":")
        caster = {"YOU": "you cast", "OPPONENT": "an opponent casts", "ANY": "a player casts"}[who]
        prefix = f"When {caster} a {what.lower().replace('_', ' ')} spell: ".replace(" spell spell", " spell")
    else:
        prefix = _TRIGGER_TEXT[ability.trigger].format(when=ability.when.lower().replace("_", " "))
    if ability.mode_group is not None:
        prefix = "Mode: " + prefix
    return prefix + body
