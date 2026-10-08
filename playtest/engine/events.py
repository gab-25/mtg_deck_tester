"""What happens in a game, as the engine reports it.

``payload`` is always JSON-safe: it is stored as is in ``MatchEvent.payload``.
"""

from dataclasses import dataclass, field


class EventKind:
    SETUP = "setup"
    MULLIGAN = "mulligan"
    TURN = "turn"
    DRAW = "draw"
    PLAY_LAND = "play_land"
    # A spell is put on the stack...
    CAST = "cast"
    # ...and resolves once every living player passes.
    RESOLVE = "resolve"
    ATTACK = "attack"
    BLOCK = "block"
    DAMAGE = "damage"
    DIES = "dies"
    # A card moves between zones as the rules say (a commander to the command zone).
    ZONE_CHANGE = "zone_change"
    DISCARD = "discard"
    # A triggered ability is put on the stack (card effects, #28).
    TRIGGER = "trigger"
    ELIMINATED = "eliminated"
    # An agent's choice, logged when it carries a stated reason.
    DECISION = "decision"
    PASS = "pass"
    AGENT_FALLBACK = "agent_fallback"
    DECISION_LIMIT = "decision_limit"
    # Any other cap of :class:`~.limits.Limits` biting.
    LIMIT = "limit"
    # Something the engine handled but should not have had to.
    WARNING = "warning"
    GAME_OVER = "game_over"


@dataclass
class Event:
    kind: str
    # The seat the event is about, or None for table-wide events.
    seat: int | None
    text: str
    payload: dict = field(default_factory=dict)
    # Why the agent chose this, when the event is the outcome of a decision.
    reasoning: str = ""
