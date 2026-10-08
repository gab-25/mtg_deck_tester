"""The state of a game in progress.

Identity follows CR 400.7: a card keeps its id for the whole game, but every
time it changes zone it becomes a new object. A permanent gets a fresh id when
it enters the battlefield, so a creature that dies and comes back has no
damage, no counters and summoning sickness again. Card, permanent and stack ids
all come from one monotone counter, so no two objects ever share an id.

No floats anywhere in the kernel.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from .cards import CardSpec, Kind
from .limits import Limits


class Step(StrEnum):
    """The steps of a turn, in order (CR 500)."""

    UNTAP = "untap"
    UPKEEP = "upkeep"
    DRAW = "draw"
    MAIN1 = "main1"
    BEGIN_COMBAT = "begin_combat"
    DECLARE_ATTACKERS = "declare_attackers"
    DECLARE_BLOCKERS = "declare_blockers"
    # Only when an attacking or blocking creature has first or double strike (CR 510.4).
    FIRST_STRIKE_DAMAGE = "first_strike_damage"
    COMBAT_DAMAGE = "combat_damage"
    END_COMBAT = "end_combat"
    MAIN2 = "main2"
    END_STEP = "end_step"
    CLEANUP = "cleanup"


MAIN_STEPS = (Step.MAIN1, Step.MAIN2)


class Declaring(StrEnum):
    ATTACKERS = "attackers"
    BLOCKERS = "blockers"


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


@dataclass(slots=True)
class Card:
    """One physical card: its id is stable for the whole game."""

    id: int
    spec: CardSpec
    owner: int
    is_commander: bool = False
    # A token ceases to exist once it leaves the battlefield (CR 704.5d).
    token: bool = False

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(slots=True)
class Permanent:
    """A card on the battlefield: a new object, with a new id, on every entry."""

    id: int
    card: Card
    controller: int
    tapped: bool = False
    # Came under its controller's control since the start of their latest turn.
    sick: bool = True
    damage: int = 0
    # Dealt damage by a source with deathtouch since the last cleanup.
    deathtouched: bool = False
    counters: dict[str, int] = field(default_factory=dict)
    # The seat it attacks, while it is an attacking creature.
    attacking: int | None = None
    # Set when blockers are declared: stays blocked even if its blockers leave.
    blocked: bool = False
    # The attacker it blocks, while it is a blocking creature.
    blocking: int | None = None
    # When its block was declared: blockers are assigned damage in this order.
    block_seq: int = 0

    @property
    def name(self) -> str:
        return self.card.spec.name

    @property
    def spec(self) -> CardSpec:
        return self.card.spec

    @property
    def is_creature(self) -> bool:
        return self.card.spec.kind == Kind.CREATURE

    @property
    def power(self) -> int:
        return self.card.spec.power

    @property
    def toughness(self) -> int:
        return self.card.spec.toughness

    def has(self, keyword: str) -> bool:
        return self.card.spec.has(keyword)

    @property
    def can_tap_for_attack_or_mana(self) -> bool:
        """Untapped, and not held back by summoning sickness (CR 302.6)."""
        return not self.tapped and not (self.is_creature and self.sick and not self.has("haste"))


@dataclass(slots=True)
class StackItem:
    id: int
    card: Card
    controller: int
    x: int = 0
    from_command_zone: bool = False


@dataclass
class PlayerState:
    seat: int
    deck_name: str
    life: int
    library: list[Card] = field(default_factory=list)
    hand: list[Card] = field(default_factory=list)
    battlefield: list[Permanent] = field(default_factory=list)
    graveyard: list[Card] = field(default_factory=list)
    exile: list[Card] = field(default_factory=list)
    command: list[Card] = field(default_factory=list)
    # What left the game with this player when they lost (CR 800.4a).
    out_of_game: list[Card] = field(default_factory=list)
    commander_casts: int = 0
    lands_played: int = 0
    # Combat damage taken from each commander, by the commander's card id.
    commander_damage: dict[int, int] = field(default_factory=dict)
    # Set when a draw found the library empty: they lose at the next SBA check.
    drew_from_empty: bool = False
    mulligans: int = 0
    eliminated_round: int | None = None

    @property
    def alive(self) -> bool:
        return self.eliminated_round is None

    def zones(self) -> tuple[list[Card], ...]:
        return (self.library, self.hand, self.graveyard, self.exile, self.command, self.out_of_game)

    def creatures(self) -> list[Permanent]:
        return [p for p in self.battlefield if p.is_creature]


@dataclass
class GameState:
    rules: GameRules
    players: list[PlayerState]
    limits: Limits = field(default_factory=Limits)
    # A round is one turn for every player still in the game.
    round: int = 0
    # The overall turn count, across players.
    turn: int = 0
    active_seat: int = 0
    step: Step | None = None
    stack: list[StackItem] = field(default_factory=list)
    next_id: int = 1
    # Who is declaring attackers or blockers right now, if anyone.
    declaring: Declaring | None = None
    declaring_seat: int | None = None
    # Cards dealt into the game; zones always add up to it.
    total_cards: int = 0
    # The owner of every commander, by card id.
    commanders: dict[int, int] = field(default_factory=dict)

    def new_id(self) -> int:
        new = self.next_id
        self.next_id += 1
        return new

    def player(self, seat: int) -> PlayerState:
        return self.players[seat]

    def alive_players(self) -> list[PlayerState]:
        return [p for p in self.players if p.alive]

    def opponents(self, seat: int) -> list[PlayerState]:
        return [p for p in self.players if p.alive and p.seat != seat]

    def seats_from(self, seat: int) -> list[int]:
        """Living seats in turn order, starting with ``seat``."""
        count = len(self.players)
        return [(seat + i) % count for i in range(count) if self.players[(seat + i) % count].alive]

    def permanents(self) -> list[Permanent]:
        return [p for player in self.players for p in player.battlefield]

    def permanent(self, pid: int) -> Permanent | None:
        return next((p for p in self.permanents() if p.id == pid), None)

    def attackers(self) -> list[Permanent]:
        return [p for p in self.permanents() if p.attacking is not None]

    def blockers_of(self, attacker_id: int) -> list[Permanent]:
        blockers = [p for p in self.permanents() if p.blocking == attacker_id]
        return sorted(blockers, key=lambda p: p.block_seq)


class InvariantError(AssertionError):
    pass


def assert_invariants(state: GameState) -> None:
    """Checks what must hold between any two actions; raises :class:`InvariantError`."""

    def fail(message: str):
        raise InvariantError(message)

    card_ids: list[int] = []
    for player in state.players:
        for zone in player.zones():
            card_ids += [c.id for c in zone]
        card_ids += [p.card.id for p in player.battlefield]
    card_ids += [item.card.id for item in state.stack]
    if len(card_ids) != len(set(card_ids)):
        fail("a card is in two places at once")
    if len(card_ids) != state.total_cards:
        fail(f"{len(card_ids)} cards in the zones, {state.total_cards} dealt")

    object_ids = [p.id for p in state.permanents()] + [s.id for s in state.stack]
    if len(object_ids) != len(set(object_ids)):
        fail("two objects share an id")
    if set(object_ids) & set(card_ids):
        fail("an object shares its id with a card")
    if any(i >= state.next_id for i in object_ids + card_ids):
        fail("an id was not handed out by the counter")

    attacking_ids = {p.id for p in state.attackers()}
    for perm in state.permanents():
        if perm.attacking is not None and perm.blocking is not None:
            fail(f"{perm.name} is both attacking and blocking")
        if perm.blocking is not None and perm.blocking not in attacking_ids:
            fail(f"{perm.name} blocks a creature that is not attacking")
        if perm.attacking is not None and perm.controller != state.active_seat:
            fail(f"{perm.name} attacks outside its controller's turn")
        if perm.damage < 0 or any(n < 0 for n in perm.counters.values()):
            fail(f"{perm.name} has a negative count")
        if perm.controller != perm.card.owner:
            fail(f"{perm.name} changed control")
        if not state.players[perm.controller].alive:
            fail(f"{perm.name} stayed behind a player who left")

    for player in state.players:
        if player.lands_played < 0 or player.commander_casts < 0 or player.mulligans < 0:
            fail(f"seat {player.seat} has a negative count")
        if any(n < 0 for n in player.commander_damage.values()):
            fail(f"seat {player.seat} has negative commander damage")
    if len(state.stack) > state.limits.max_stack_depth:
        fail("the stack is deeper than its limit")
