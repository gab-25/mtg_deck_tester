"""Moving objects between zones.

Every move off the battlefield ends the permanent (its id is never reused);
every move onto it creates a new one (CR 400.7). A commander that would go to
a graveyard or to exile goes to its owner's command zone instead (CR 903.9),
and a token that leaves the battlefield simply ceases to exist.
"""

from .cards import CardSpec
from .dsl import Trigger
from .events import Event, EventKind
from .state import Card, GameState, PendingTrigger, Permanent


class Zone:
    GRAVEYARD = "graveyard"
    EXILE = "exile"
    HAND = "hand"
    LIBRARY_TOP = "library"
    COMMAND = "command"


def queue_triggers(state: GameState, perm: Permanent, trigger: str) -> None:
    for ability in perm.spec.abilities(trigger):
        state.triggers.append(PendingTrigger(perm.controller, perm.name, perm.id, ability))


def enter_battlefield(state: GameState, card: Card | None, controller: int, spec: CardSpec | None = None) -> Permanent:
    """Puts ``card`` (or a token of ``spec``) onto the battlefield as a new object."""
    spec = spec or card.spec
    perm = Permanent(
        id=state.new_id(),
        spec=spec,
        controller=controller,
        owner=card.owner if card is not None else controller,
        card=card,
        tapped=spec.enters_tapped,
        sick=True,
    )
    state.battlefield.append(perm)
    queue_triggers(state, perm, Trigger.ETB)
    return perm


def _detach_from_combat(state: GameState, perm: Permanent) -> None:
    if perm.blocking is not None:
        attacker = state.permanent(perm.blocking)
        if attacker is not None and perm.id in attacker.blocked_by:
            attacker.blocked_by.remove(perm.id)
    for blocker_id in perm.blocked_by:
        blocker = state.permanent(blocker_id)
        if blocker is not None:
            blocker.blocking = None


def put_card(state: GameState, card: Card, zone: str) -> str:
    """Puts a card that is in no zone into ``zone`` of its owner; returns where it went."""
    owner = state.player(card.owner)
    if card.is_commander and zone in (Zone.GRAVEYARD, Zone.EXILE):
        zone = Zone.COMMAND
    target = {
        Zone.GRAVEYARD: owner.graveyard,
        Zone.EXILE: owner.exile,
        Zone.HAND: owner.hand,
        Zone.COMMAND: owner.command,
    }.get(zone)
    if zone == Zone.LIBRARY_TOP:
        owner.library.insert(0, card)
    else:
        target.append(card)
    return zone


def leave_battlefield(state: GameState, perm: Permanent, zone: str) -> list[Event]:
    """Moves ``perm`` off the battlefield to its owner's ``zone``."""
    if perm not in state.battlefield:
        return []
    _detach_from_combat(state, perm)
    state.battlefield.remove(perm)
    events = []
    if zone == Zone.GRAVEYARD and perm.spec.is_creature:
        # "Dies" triggers look back at the object as it last existed.
        queue_triggers(state, perm, Trigger.DIES)
        events.append(
            Event(EventKind.DIES, perm.controller, f"{perm.name} dies.", {"card": perm.name, "token": perm.is_token})
        )
    if perm.card is not None:
        landed = put_card(state, perm.card, zone)
        if landed == Zone.COMMAND and zone != Zone.COMMAND:
            events.append(
                Event(
                    EventKind.EFFECT,
                    perm.owner,
                    f"{perm.name} returns to the command zone.",
                    {"card": perm.name, "zone": Zone.COMMAND},
                )
            )
    return events


def take_from(zone: list[Card], card_id: int) -> Card | None:
    for index, card in enumerate(zone):
        if card.id == card_id:
            return zone.pop(index)
    return None


def remove_player_objects(state: GameState, seat: int) -> None:
    """CR 800.4a: a player who leaves the game takes every object they own with them.

    The cards stay accounted for in their owner's exile, out of everyone's reach.
    """
    player = state.player(seat)
    for perm in [p for p in state.battlefield if p.owner == seat or p.controller == seat]:
        _detach_from_combat(state, perm)
        state.battlefield.remove(perm)
        if perm.card is not None:
            state.player(perm.card.owner).exile.append(perm.card)
    for item in [s for s in state.stack if s.controller == seat or (s.card is not None and s.card.owner == seat)]:
        state.stack.remove(item)
        if item.card is not None:
            state.player(item.card.owner).exile.append(item.card)
    state.triggers = [t for t in state.triggers if t.controller != seat]
    for perm in state.battlefield:
        if perm.attacking == seat:
            perm.attacking = None
    player.mana_pool.clear()
