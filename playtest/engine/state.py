"""The state of a game in progress.

Identity follows CR 400.7: a :class:`Card` keeps its id for the whole game,
while a :class:`Permanent` gets a fresh id every time it enters the
battlefield — a creature that dies and comes back is a new object, with no
damage, no counters, and summoning sickness again. Every id (cards,
permanents, stack items) comes from one monotone counter.

No floats anywhere in the kernel.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from .cards import CardSpec
from .dsl import Ability


class Step(StrEnum):
    UNTAP = "untap"
    UPKEEP = "upkeep"
    DRAW = "draw"
    MAIN1 = "main1"
    BEGIN_COMBAT = "begin_combat"
    DECLARE_ATTACKERS = "declare_attackers"
    DECLARE_BLOCKERS = "declare_blockers"
    FIRST_STRIKE_DAMAGE = "first_strike_damage"
    COMBAT_DAMAGE = "combat_damage"
    END_COMBAT = "end_combat"
    MAIN2 = "main2"
    END_STEP = "end_step"
    CLEANUP = "cleanup"


STEPS = tuple(Step)
MAIN_STEPS = (Step.MAIN1, Step.MAIN2)


@dataclass(frozen=True)
class GameRules:
    """The per-format parameters of a game."""

    starting_life: int
    # Combat damage from one commander that eliminates a player (None: untracked).
    commander_damage_limit: int | None
    opening_hand: int = 7
    lands_per_turn: int = 1
    # Each previous cast from the command zone adds this much to the commander's cost.
    commander_tax: int = 2
    max_hand_size: int = 7
    # Shown to agents ("a game of Magic: The Gathering (Modern)").
    format_name: str = "Commander"


@dataclass(slots=True)
class Card:
    """One physical card; its id is stable for the whole game."""

    id: int
    spec: CardSpec
    owner: int
    is_commander: bool = False

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(slots=True)
class Permanent:
    """An object on the battlefield. A fresh id on every zone change (CR 400.7)."""

    id: int
    spec: CardSpec
    controller: int
    owner: int
    # None for a token: it has no card and ceases to exist off the battlefield.
    card: Card | None = None
    tapped: bool = False
    # Came under its controller's control since their last turn began.
    sick: bool = True
    damage: int = 0
    # Dealt damage by a source with deathtouch since the last cleanup.
    deathtouched: bool = False
    plus_counters: int = 0
    minus_counters: int = 0
    # Until-end-of-turn modifications.
    eot_power: int = 0
    eot_toughness: int = 0
    eot_keywords: list[str] = field(default_factory=list)
    # Combat: the seat this creature attacks, or the attacker it blocks.
    attacking: int | None = None
    blocked_by: list[int] = field(default_factory=list)
    blocking: int | None = None
    # Whether it was blocked this combat (a blocked attacker stays blocked
    # even if its blockers leave).
    was_blocked: bool = False
    activations: int = 0

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_token(self) -> bool:
        return self.card is None

    @property
    def is_commander(self) -> bool:
        return self.card is not None and self.card.is_commander


@dataclass(slots=True)
class StackItem:
    """A spell or an ability waiting on the stack."""

    id: int
    controller: int
    # A spell carries its card; an ability carries its source's name and id.
    card: Card | None
    source_name: str
    source_id: int | None
    abilities: tuple[Ability, ...]
    # Target key -> chosen refs ("p<seat>", "o<permanent id>", "s<stack id>").
    targets: dict[str, str] = field(default_factory=dict)
    x: int = 0
    # The mana value of the source when it was cast (for "spell" selectors).
    is_creature_spell: bool = False

    @property
    def is_spell(self) -> bool:
        return self.card is not None

    @property
    def name(self) -> str:
        return self.card.name if self.card is not None else self.source_name


@dataclass(slots=True)
class PendingTrigger:
    """A triggered ability waiting to be put on the stack."""

    controller: int
    source_name: str
    source_id: int | None
    ability: Ability


@dataclass(slots=True)
class Player:
    seat: int
    name: str
    life: int
    library: list[Card] = field(default_factory=list)
    hand: list[Card] = field(default_factory=list)
    graveyard: list[Card] = field(default_factory=list)
    exile: list[Card] = field(default_factory=list)
    command: list[Card] = field(default_factory=list)
    commander_casts: int = 0
    lands_played: int = 0
    # Combat damage taken from each opposing commander, by its owner's seat.
    commander_damage: dict[int, int] = field(default_factory=dict)
    eliminated_round: int | None = None
    # Set when a draw found the library empty (checked by state-based actions).
    drew_from_empty: bool = False
    # Mana added by spells and abilities; emptied at the end of every step.
    mana_pool: dict[str, int] = field(default_factory=dict)
    # Dead spells already reported as such (one UNSUPPORTED_SKIPPED each).
    reported_dead: set[int] = field(default_factory=set)
    # Unsupported instants and sorceries that reached this player's hand.
    dead_drawn: int = 0

    @property
    def alive(self) -> bool:
        return self.eliminated_round is None


@dataclass(slots=True)
class Combat:
    """The current declaration, when an agent is declaring attackers or blockers."""

    # "attackers", or "blockers" for ``defender``.
    phase: str = ""
    defender: int | None = None
    steps: int = 0


@dataclass(slots=True)
class PendingTarget:
    """A stack item still waiting for one of its targets to be chosen."""

    item_id: int
    key: str


@dataclass(slots=True)
class GameState:
    rules: "GameRules"
    players: list[Player]
    battlefield: list[Permanent] = field(default_factory=list)
    stack: list[StackItem] = field(default_factory=list)
    triggers: list[PendingTrigger] = field(default_factory=list)
    # A round is one turn for every player still in the game.
    round: int = 0
    # The overall turn count, across players.
    turn: int = 0
    active_seat: int = 0
    step: Step = Step.UNTAP
    # Turn order (seats), fixed at the start of the game.
    order: list[int] = field(default_factory=list)
    combat: Combat | None = None
    pending_target: PendingTarget | None = None
    next_id: int = 1

    def new_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def player(self, seat: int) -> Player:
        return self.players[seat]

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def opponents(self, seat: int) -> list[Player]:
        return [p for p in self.players if p.alive and p.seat != seat]

    def turn_order_from(self, seat: int) -> list[int]:
        """Living seats in turn order, starting with ``seat`` (APNAP order)."""
        order = self.order or list(range(len(self.players)))
        start = order.index(seat)
        return [s for s in order[start:] + order[:start] if self.players[s].alive]

    def permanent(self, permanent_id: int) -> Permanent | None:
        return next((p for p in self.battlefield if p.id == permanent_id), None)

    def stack_item(self, item_id: int) -> StackItem | None:
        return next((s for s in self.stack if s.id == item_id), None)

    def controlled(self, seat: int) -> list[Permanent]:
        return [p for p in self.battlefield if p.controller == seat]

    @property
    def sorcery_window(self) -> bool:
        return self.step in MAIN_STEPS and not self.stack and self.combat is None


class InvariantError(AssertionError):
    """The kernel left the state inconsistent: a kernel bug, never an agent's."""


def assert_invariants(state: GameState, cards_per_seat: dict[int, int]) -> None:
    """Checks what must hold after every action; raises :class:`InvariantError`."""
    ids = [p.id for p in state.battlefield] + [s.id for s in state.stack]
    if len(ids) != len(set(ids)):
        raise InvariantError("duplicate permanent or stack ids")
    seen_cards: dict[int, int] = {}
    on_field = {p.card.id for p in state.battlefield if p.card is not None}
    on_stack = {s.card.id for s in state.stack if s.card is not None}
    for seat, player in enumerate(state.players):
        zones = player.library + player.hand + player.graveyard + player.exile + player.command
        count = len(zones)
        for card in zones:
            seen_cards[card.id] = seen_cards.get(card.id, 0) + 1
        count += sum(1 for p in state.battlefield if p.card is not None and p.card.owner == seat)
        count += sum(1 for s in state.stack if s.card is not None and s.card.owner == seat)
        if count != cards_per_seat[seat]:
            raise InvariantError(f"seat {seat} has {count} cards, expected {cards_per_seat[seat]}")
        if player.life != int(player.life) or any(v < 0 for v in player.mana_pool.values()):
            raise InvariantError(f"seat {seat} has an invalid life or mana pool")
    for card_id in list(on_field) + list(on_stack):
        seen_cards[card_id] = seen_cards.get(card_id, 0) + 1
    duplicated = [cid for cid, n in seen_cards.items() if n > 1]
    if duplicated:
        raise InvariantError(f"cards in two zones at once: {duplicated}")
    for perm in state.battlefield:
        if perm.attacking is not None and perm.blocking is not None:
            raise InvariantError(f"{perm.name} is both attacking and blocking")
        if min(perm.damage, perm.plus_counters, perm.minus_counters, perm.activations) < 0:
            raise InvariantError(f"{perm.name} has a negative count")
        if not state.players[perm.controller].alive:
            raise InvariantError(f"{perm.name} is controlled by a player who left the game")
