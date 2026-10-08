"""The rules the engine enforces — and, by omission, the ones it does not.

Turn structure. Every turn runs the steps of a real turn: untap, upkeep, draw,
first main phase, beginning of combat, declare attackers, declare blockers,
(first-strike damage), combat damage, end of combat, second main phase, end
step, cleanup. The first player of a two-player game skips their first draw.

Priority and the stack. In every step but untap and cleanup the active player
gets priority first, then it passes around the table in turn order. Casting a
spell puts it on the stack and the caster gets priority again. The top of the
stack resolves, or the step ends, only when every living player passes in
succession. A player with nothing to do but pass passes automatically.

Timing. Lands are played, and sorcery-speed spells cast, only by the active
player in a main phase with an empty stack; one land per turn. Instants and
cards with flash can be cast whenever their controller has priority.

Mana. Every land and mana rock makes the colors Scryfall says it makes, in the
amount its mana ability says (Sol Ring makes two). Creatures can't tap for mana
the turn they arrive unless they have haste. Payment is NOT a decision: the
engine taps sources itself, choosing the ones least worth keeping — so a green
spell never taps your only blue source when another source can pay. The flip
side: you can't choose to keep a particular land untapped (no "holding up
Island" for a counterspell). {X} spells are offered once per value of X, from 0
up to the mana available (at most 10).

Card effects. Not wired yet: spells and abilities resolve with no effect.
Creatures, artifacts, enchantments and other permanents enter the battlefield;
instants and sorceries go to the graveyard. Keywords the engine knows: flying,
reach, menace, first strike, double strike, trample, deathtouch, lifelink,
vigilance, haste, defender, flash, indestructible.

The commander starts in the command zone and is cast from there for its cost
plus 2 for every previous cast from the command zone. When it would stay in a
graveyard or in exile, it returns to the command zone.

Combat. Attackers are declared one at a time, each at an opponent of your
choice, so attacks can be split across opponents. A creature can't attack the
turn it arrives unless it has haste; attacking taps it unless it has
vigilance. Each defending player, in turn order, declares blockers one at a
time for the creatures attacking them, and can take a block back. A creature
with flying can be blocked only by creatures with flying or reach; one with
menace only by two or more. First strike and double strike deal damage in a
separate, earlier step. A blocked attacker assigns lethal damage to its
blockers in the order they blocked; with trample the rest goes to the player.
An unblocked attacker deals its damage to the player it attacks.

State-based actions, checked until nothing changes before anyone gets
priority: a player at 0 or less life loses; a player who drew from an empty
library loses; in Commander, a player dealt 21 combat damage by a single
commander loses; a creature with 0 toughness, or with lethal damage (unless
indestructible), dies; of two legendary permanents with the same name, the
newer goes to the graveyard. A player who loses leaves the game with
everything they own, spells on the stack included; if it was their turn, the
turn ends.

Setup. Everyone draws seven and keeps a hand with 2 to 5 lands; otherwise
they take a London mulligan, at most twice, the first one free with three or
more players. Mulligans are not a decision either.
"""

from .actions import Action, ActionKind, legal_actions
from .cards import Kind
from .events import Event, EventKind
from .mana import mana_sources, payment
from .state import Card, GameState, Permanent, PlayerState, StackItem, Step


class IllegalAction(Exception):
    """An agent tried something the rules do not allow."""


# -- zones ---------------------------------------------------------------------


def enter_battlefield(state: GameState, card: Card, controller: int) -> Permanent:
    perm = Permanent(state.new_id(), card, controller, tapped=card.spec.enters_tapped)
    state.player(controller).battlefield.append(perm)
    return perm


def _put_in_graveyard(state: GameState, card: Card) -> None:
    state.player(card.owner).graveyard.append(card)


def _clear_dangling_blocks(state: GameState) -> None:
    attacking = {p.id for p in state.attackers()}
    for perm in state.permanents():
        if perm.blocking is not None and perm.blocking not in attacking:
            perm.blocking = None


def leave_battlefield(state: GameState, perm: Permanent) -> Card:
    """Takes ``perm`` off the battlefield; its card is a new object wherever it goes."""
    state.player(perm.controller).battlefield.remove(perm)
    perm.attacking = None
    perm.blocking = None
    _clear_dangling_blocks(state)
    return perm.card


# -- turn-based actions --------------------------------------------------------


def untap_step(state: GameState) -> None:
    player = state.player(state.active_seat)
    for perm in player.battlefield:
        perm.tapped = False
        perm.sick = False
    player.lands_played = 0


def draw(state: GameState, seat: int) -> list[Event]:
    """``seat`` draws a card; from an empty library they lose at the next SBA check."""
    player = state.player(seat)
    if not player.library:
        player.drew_from_empty = True
        return [
            Event(
                EventKind.DRAW,
                seat,
                f"{player.deck_name} has to draw from an empty library.",
                {"empty_library": True},
            )
        ]
    player.hand.append(player.library.pop(0))
    return [Event(EventKind.DRAW, seat, f"{player.deck_name} draws a card.", {"library": len(player.library)})]


def cleanup(state: GameState) -> list[Event]:
    """Discard to hand size, then damage wears off."""
    events = []
    player = state.player(state.active_seat)
    excess = len(player.hand) - state.rules.max_hand_size
    if player.alive and excess > 0:
        # The highest mana values go first: they are the hardest to cast.
        ranked = sorted(player.hand, key=lambda c: (-c.spec.mana_value, c.name, c.id))
        for card in ranked[:excess]:
            player.hand.remove(card)
            _put_in_graveyard(state, card)
        names = [c.name for c in ranked[:excess]]
        events.append(
            Event(
                EventKind.DISCARD,
                player.seat,
                f"{player.deck_name} discards {', '.join(names)} down to {state.rules.max_hand_size} cards.",
                {"cards": names},
            )
        )
    for perm in state.permanents():
        perm.damage = 0
        perm.deathtouched = False
    return events


def opening_hand(state: GameState, player: PlayerState, rng, free_mulligan: bool) -> list[Event]:
    """Deals ``player`` a hand they keep, with deterministic London mulligans."""
    size = state.rules.opening_hand
    for attempt in range(3):
        player.hand = player.library[:size]
        del player.library[:size]
        lands = sum(1 for c in player.hand if c.spec.kind == Kind.LAND)
        if 2 <= lands <= 5 or attempt == 2:
            break
        player.library += player.hand
        player.hand = []
        rng.shuffle(player.library)
        player.mulligans += 1
    if not player.mulligans:
        return []

    to_bottom = max(0, player.mulligans - (1 if free_mulligan else 0))
    bottomed = []
    for _ in range(min(to_bottom, len(player.hand))):
        lands = sorted((c for c in player.hand if c.spec.kind == Kind.LAND), key=lambda c: (c.name, c.id))
        spells = sorted(
            (c for c in player.hand if c.spec.kind != Kind.LAND),
            key=lambda c: (-c.spec.mana_value, c.name, c.id),
        )
        card = lands[-1] if len(lands) > 3 or not spells else spells[0]
        player.hand.remove(card)
        player.library.append(card)
        bottomed.append(card.name)
    times = "once" if player.mulligans == 1 else "twice"
    return [
        Event(
            EventKind.MULLIGAN,
            player.seat,
            f"{player.deck_name} mulligans {times} and keeps {len(player.hand)} cards.",
            {"mulligans": player.mulligans, "kept": len(player.hand), "bottom": bottomed},
        )
    ]


# -- actions -------------------------------------------------------------------


def apply(state: GameState, seat: int, action: Action) -> list[Event]:
    """Carries out ``action`` for ``seat`` and returns what happened.

    Raises :class:`IllegalAction` unless ``action`` is one of
    ``legal_actions(state, seat)``.
    """
    if action not in legal_actions(state, seat):
        raise IllegalAction(f"Not a legal action right now: {action}")
    player = state.player(seat)
    kind = action.kind

    if kind == ActionKind.PASS:
        return []
    if kind == ActionKind.PLAY_LAND:
        card = _take(player.hand, action.card_uid)
        player.lands_played += 1
        perm = enter_battlefield(state, card, seat)
        tapped = " tapped" if perm.tapped else ""
        return [
            Event(
                EventKind.PLAY_LAND,
                seat,
                f"{player.deck_name} plays {card.name}{tapped}.",
                {"card": card.name, "card_id": card.id},
            )
        ]
    if kind in (ActionKind.CAST_SPELL, ActionKind.CAST_COMMANDER):
        return _cast(state, seat, action)
    if kind == ActionKind.DECLARE_ATTACKER:
        state.permanent(action.card_uid).attacking = action.target_seat
        return []
    if kind == ActionKind.END_ATTACKERS:
        return _end_attackers(state, seat)
    if kind == ActionKind.DECLARE_BLOCKER:
        blocker = state.permanent(action.card_uid)
        blocker.blocking = action.target_uid
        blocker.block_seq = 1 + max((p.block_seq for p in state.permanents()), default=0)
        return []
    if kind == ActionKind.UNDO_BLOCKER:
        blocker = state.permanent(action.card_uid)
        blocker.blocking = None
        blocker.block_seq = 0
        return []
    if kind == ActionKind.END_BLOCKERS:
        return _end_blockers(state, seat)
    raise IllegalAction(f"Unknown action {kind!r}.")


def _take(zone: list[Card], card_id: int) -> Card:
    for idx, card in enumerate(zone):
        if card.id == card_id:
            return zone.pop(idx)
    raise IllegalAction(f"No card with id {card_id} in that zone.")


def _cast(state: GameState, seat: int, action: Action) -> list[Event]:
    player = state.player(seat)
    from_command = action.kind == ActionKind.CAST_COMMANDER
    zone = player.command if from_command else player.hand
    card = next(c for c in zone if c.id == action.card_uid)
    cost = card.spec.mana_cost
    x = action.x or 0
    tax = state.rules.commander_tax * player.commander_casts if from_command else 0
    tapped = payment(mana_sources(player), cost, tax + x * cost.x)
    if tapped is None:
        raise IllegalAction(f"Cannot pay for {card.name}.")
    for source in tapped:
        state.permanent(source.id).tapped = True
    _take(zone, card.id)
    if from_command:
        player.commander_casts += 1
    state.stack.append(StackItem(state.new_id(), card, seat, x, from_command))

    x_text = f" with X={x}" if cost.x else ""
    where = " from the command zone" if from_command else ""
    paid = f", tapping {', '.join(s.name for s in tapped)}" if tapped else ""
    return [
        Event(
            EventKind.CAST,
            seat,
            f"{player.deck_name} casts {card.name}{x_text}{where}{paid}.",
            {
                "card": card.name,
                "card_id": card.id,
                "mana_value": cost.mana_value,
                "x": x,
                "commander": card.is_commander,
                "tax": tax,
                "tapped": [s.name for s in tapped],
                "stack_depth": len(state.stack),
            },
        )
    ]


def resolve_top(state: GameState) -> list[Event]:
    item = state.stack.pop()
    card = item.card
    owner = state.player(item.controller)
    if card.spec.kind in (Kind.CREATURE, Kind.PERMANENT):
        perm = enter_battlefield(state, card, item.controller)
        text = f"{card.name} resolves and enters the battlefield under {owner.deck_name}'s control"
        text += " tapped." if perm.tapped else "."
        payload = {"card": card.name, "card_id": card.id, "permanent_id": perm.id}
    else:
        _put_in_graveyard(state, card)
        text = f"{card.name} resolves (card effects are not simulated yet)."
        payload = {"card": card.name, "card_id": card.id}
    return [Event(EventKind.RESOLVE, item.controller, text, payload)]


def _end_attackers(state: GameState, seat: int) -> list[Event]:
    player = state.player(seat)
    attackers = [p for p in player.battlefield if p.attacking is not None]
    for perm in attackers:
        if not perm.has("vigilance"):
            perm.tapped = True
    events = []
    for defender in state.seats_from(seat):
        group = [p for p in attackers if p.attacking == defender]
        if not group:
            continue
        target = state.player(defender)
        events.append(
            Event(
                EventKind.ATTACK,
                seat,
                f"{player.deck_name} attacks {target.deck_name} with "
                + ", ".join(f"{p.name} ({p.power}/{p.toughness})" for p in group)
                + ".",
                {
                    "target_seat": defender,
                    "attackers": [{"id": p.id, "name": p.name, "power": p.power} for p in group],
                },
            )
        )
    return events


def _end_blockers(state: GameState, seat: int) -> list[Event]:
    player = state.player(seat)
    blocks = []
    for attacker in state.attackers():
        if attacker.attacking != seat:
            continue
        blockers = state.blockers_of(attacker.id)
        if blockers:
            attacker.blocked = True
            blocks.append((attacker, blockers))
    if not blocks:
        text = f"{player.deck_name} does not block."
    else:
        text = f"{player.deck_name} blocks " + "; ".join(
            f"{a.name} with {', '.join(b.name for b in bs)}" for a, bs in blocks
        ) + "."
    return [
        Event(
            EventKind.BLOCK,
            seat,
            text,
            {
                "blocks": [
                    {"attacker": a.name, "attacker_id": a.id, "blockers": [b.id for b in bs]}
                    for a, bs in blocks
                ]
            },
        )
    ]


def force_end_declaration(state: GameState, seat: int) -> list[Event]:
    """Ends a declaration that hit its cap: drops lone blocks on menace attackers, then ends it."""
    for attacker in state.attackers():
        blockers = state.blockers_of(attacker.id)
        if attacker.has("menace") and len(blockers) == 1:
            blockers[0].blocking = None
            blockers[0].block_seq = 0
    if state.declaring == "attackers":
        return _end_attackers(state, seat)
    return _end_blockers(state, seat)


# -- combat damage -------------------------------------------------------------


def _strikes_first(perm: Permanent) -> bool:
    return perm.has("first strike") or perm.has("double strike")


def has_first_strike_step(state: GameState) -> bool:
    combatants = [p for p in state.permanents() if p.attacking is not None or p.blocking is not None]
    return any(_strikes_first(p) for p in combatants)


def _deals_damage_now(perm: Permanent, step: Step) -> bool:
    if step == Step.FIRST_STRIKE_DAMAGE:
        return _strikes_first(perm)
    return perm.has("double strike") or not perm.has("first strike")


def _lethal(perm: Permanent, source: Permanent) -> int:
    if source.has("deathtouch"):
        return 1
    return max(perm.toughness - perm.damage, 0)


def combat_damage(state: GameState, step: Step) -> list[Event]:
    """Every attacker and blocker that deals damage in ``step`` deals it, all at once."""
    # (source, target permanent or None, target seat or None, amount)
    hits: list[tuple[Permanent, Permanent | None, int | None, int]] = []
    for attacker in state.attackers():
        if not _deals_damage_now(attacker, step) or attacker.power <= 0:
            continue
        if not attacker.blocked:
            hits.append((attacker, None, attacker.attacking, attacker.power))
            continue
        remaining = attacker.power
        blockers = state.blockers_of(attacker.id)
        for blocker in blockers:
            share = min(remaining, _lethal(blocker, attacker))
            if share:
                hits.append((attacker, blocker, None, share))
                remaining -= share
        if remaining and attacker.has("trample"):
            hits.append((attacker, None, attacker.attacking, remaining))
        elif remaining and blockers:
            # No trample: whatever is left goes to the last blocker.
            hits.append((attacker, blockers[-1], None, remaining))
    for blocker in state.permanents():
        if blocker.blocking is None or not _deals_damage_now(blocker, step) or blocker.power <= 0:
            continue
        hits.append((blocker, state.permanent(blocker.blocking), None, blocker.power))

    events = []
    for source, target, seat, amount in hits:
        controller = state.player(source.controller)
        if target is not None:
            target.damage += amount
            if source.has("deathtouch"):
                target.deathtouched = True
            events.append(
                Event(
                    EventKind.DAMAGE,
                    target.controller,
                    f"{source.name} deals {amount} damage to {target.name}.",
                    {"source": source.name, "source_id": source.id, "target": target.name,
                     "target_id": target.id, "amount": amount},
                )
            )
        else:
            player = state.player(seat)
            player.life -= amount
            if source.card.is_commander:
                key = source.card.id
                player.commander_damage[key] = player.commander_damage.get(key, 0) + amount
            events.append(
                Event(
                    EventKind.DAMAGE,
                    seat,
                    f"{source.name} deals {amount} damage to {player.deck_name} ({player.life} life left).",
                    {"source": source.name, "source_id": source.id, "target_seat": seat,
                     "amount": amount, "life": player.life, "commander": source.card.is_commander},
                )
            )
        if source.has("lifelink"):
            controller.life += amount
            events.append(
                Event(
                    EventKind.DAMAGE,
                    controller.seat,
                    f"{controller.deck_name} gains {amount} life from {source.name} ({controller.life} life).",
                    {"lifelink": True, "source": source.name, "amount": amount, "life": controller.life},
                )
            )
    return events


def end_combat(state: GameState) -> None:
    for perm in state.permanents():
        perm.attacking = None
        perm.blocked = False
        perm.blocking = None
        perm.block_seq = 0


# -- state-based actions -------------------------------------------------------


def lose(state: GameState, seat: int, reason: str) -> list[Event]:
    """``seat`` loses and leaves the game with everything they own (CR 800.4a)."""
    player = state.player(seat)
    if not player.alive:
        return []
    player.eliminated_round = state.round
    for perm in list(player.battlefield):
        player.out_of_game.append(leave_battlefield(state, perm))
    for item in [i for i in state.stack if i.card.owner == seat or i.controller == seat]:
        state.stack.remove(item)
        state.player(item.card.owner).out_of_game.append(item.card)
    # Creatures attacking a player who left are removed from combat.
    for perm in state.permanents():
        if perm.attacking == seat:
            perm.attacking = None
            perm.blocked = False
    _clear_dangling_blocks(state)
    return [Event(EventKind.ELIMINATED, seat, f"{player.deck_name} is eliminated: {reason}.", {"reason": reason})]


def _sba_pass(state: GameState) -> list[Event]:
    """One simultaneous round of state-based actions; empty when nothing applied."""
    events: list[Event] = []
    limit = state.rules.commander_damage_limit

    losers = []
    for player in state.alive_players():
        if player.life <= 0:
            losers.append((player.seat, "life total reached 0"))
        elif player.drew_from_empty:
            losers.append((player.seat, "drew from an empty library"))
        elif limit is not None:
            for commander_id, damage in sorted(player.commander_damage.items()):
                if damage >= limit:
                    name = _card_name(state, commander_id)
                    losers.append((player.seat, f"{limit} combat damage from the commander {name}"))
                    break

    dying: list[tuple[Permanent, str]] = []
    for perm in state.permanents():
        if not perm.is_creature:
            continue
        if perm.toughness <= 0:
            dying.append((perm, "0 toughness"))
        elif (perm.damage >= perm.toughness or perm.deathtouched and perm.damage > 0) and not perm.has(
            "indestructible"
        ):
            dying.append((perm, "lethal damage"))
    for player in state.players:
        seen: dict[str, Permanent] = {}
        for perm in sorted(player.battlefield, key=lambda p: p.id):
            if not perm.spec.legendary:
                continue
            if perm.name in seen and all(perm is not d for d, _ in dying):
                dying.append((perm, "legend rule"))
            seen.setdefault(perm.name, perm)

    for perm, why in dying:
        card = leave_battlefield(state, perm)
        _put_in_graveyard(state, card)
        verb = "dies" if perm.is_creature and why != "legend rule" else "is put into the graveyard"
        events.append(
            Event(
                EventKind.DIES,
                perm.controller,
                f"{perm.name} {verb} ({why}).",
                {"card": perm.name, "card_id": card.id, "permanent_id": perm.id, "reason": why},
            )
        )

    for player in state.players:
        for zone in (player.graveyard, player.exile, player.hand, player.library):
            for card in [c for c in zone if c.token]:
                zone.remove(card)
                state.total_cards -= 1
                events.append(Event(EventKind.ZONE_CHANGE, player.seat, f"The token {card.name} ceases to exist."))
        for zone in (player.graveyard, player.exile):
            for card in [c for c in zone if c.is_commander]:
                zone.remove(card)
                player.command.append(card)
                events.append(
                    Event(
                        EventKind.ZONE_CHANGE,
                        player.seat,
                        f"{card.name} returns to the command zone.",
                        {"card": card.name, "card_id": card.id, "to": "command"},
                    )
                )

    for seat, reason in losers:
        events += lose(state, seat, reason)
    return events


def _card_name(state: GameState, card_id: int) -> str:
    for player in state.players:
        for zone in player.zones():
            for card in zone:
                if card.id == card_id:
                    return card.name
        for perm in player.battlefield:
            if perm.card.id == card_id:
                return perm.name
    return next((i.card.name for i in state.stack if i.card.id == card_id), "?")


def check_state_based_actions(state: GameState) -> tuple[list[Event], bool]:
    """Runs SBAs to a fixpoint: ``(events, settled)``; not settled when the cap bit."""
    events = []
    for _ in range(state.limits.max_sba_iterations):
        found = _sba_pass(state)
        if not found:
            return events, True
        events += found
    return events, False


def end_turn_early(state: GameState) -> list[Event]:
    """The turn ends now: spells on the stack leave it, combat ends."""
    events = []
    while state.stack:
        item = state.stack.pop()
        _put_in_graveyard(state, item.card)
        events.append(
            Event(
                EventKind.ZONE_CHANGE,
                item.controller,
                f"{item.card.name} leaves the stack as the turn ends.",
                {"card": item.card.name, "card_id": item.card.id, "to": "graveyard"},
            )
        )
    end_combat(state)
    state.declaring = None
    state.declaring_seat = None
    return events
