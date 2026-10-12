"""What happens in a game, as the engine reports it."""

from dataclasses import dataclass, field


class EventKind:
    SETUP = "setup"
    MULLIGAN = "mulligan"
    TURN = "turn"
    DRAW = "draw"
    PLAY_LAND = "play_land"
    # A spell put on the stack, and what happens when it leaves it.
    CAST = "cast"
    RESOLVE = "resolve"
    COUNTERED = "countered"
    # Every target became illegal: the spell or ability does nothing.
    FIZZLE = "fizzle"
    ACTIVATE = "activate"
    TRIGGER = "trigger"
    TARGET = "target"
    # An effect's outcome (life gained, cards drawn, a creature destroyed…).
    EFFECT = "effect"
    ATTACK = "attack"
    BLOCK = "block"
    DAMAGE = "damage"
    DIES = "dies"
    DISCARD = "discard"
    ELIMINATED = "eliminated"
    # An agent's choice, logged when it carries a reason or a confidence.
    DECISION = "decision"
    PASS = "pass"
    AGENT_FALLBACK = "agent_fallback"
    DECISION_LIMIT = "decision_limit"
    # A kernel guard tripped (stack depth, options per decision…).
    LIMIT = "limit"
    WARNING = "warning"
    # An unsupported instant or sorcery could have been cast but is a blank.
    UNSUPPORTED_SKIPPED = "unsupported_skipped"
    GAME_OVER = "game_over"


@dataclass
class Event:
    kind: str
    # The seat the event is about, or None for table-wide events.
    seat: int | None
    text: str
    # Always JSON-safe: it is stored in ``MatchEvent.payload``.
    payload: dict = field(default_factory=dict)
    # Why the agent chose this, when the event is the outcome of a decision.
    reasoning: str = ""
