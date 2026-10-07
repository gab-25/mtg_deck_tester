"""Runs a stored match through the engine and stores what happened."""

import logging

from django.db import transaction
from django.utils import timezone

from decks.formats import FORMATS
from decks.rules.cards import classify_card
from mtg_deck_tester.logging_context import job_log_context

from .agents.llm_agent import LLMAgent
from .agents.random_agent import RandomAgent
from .engine.cardrules import printed_stat
from .engine.cards import CardSpec, DeckSpec, Kind
from .engine.game import Game
from .engine.state import GameRules
from .models import Match, MatchEvent, Seat

logger = logging.getLogger(__name__)

_KINDS = {
    "Land": Kind.LAND,
    "Creature": Kind.CREATURE,
    "Artifact": Kind.PERMANENT,
    "Enchantment": Kind.PERMANENT,
    "Planeswalker": Kind.PERMANENT,
    "Battle": Kind.PERMANENT,
}

def card_spec(data: dict) -> CardSpec:
    """Turns a stored (processed Scryfall) card into what the engine plays with."""
    return CardSpec(
        name=data.get("name", "Unknown card"),
        kind=_KINDS.get(classify_card(data), Kind.SPELL),
        mana_value=int(data.get("cmc") or 0),
        power=printed_stat(data.get("power")),
        toughness=printed_stat(data.get("toughness")),
    )


def deck_spec(name: str, cards: list) -> DeckSpec:
    """Expands a deck's stored ``cards`` into its commander and its library."""
    commander = None
    library = []
    for item in cards:
        spec = card_spec(item["data"])
        if item.get("is_commander") and commander is None:
            commander = spec
        else:
            library.extend([spec] * item["quantity"])
    if commander is None:
        raise ValueError(f"The deck {name!r} has no commander.")
    return DeckSpec(name=name, commander=commander, library=tuple(library))


def table_names(seats: list) -> list[str]:
    """The name each seat plays under: its deck's, plus the seat when a deck sits twice."""
    names = [seat.deck_name for seat in seats]
    return [
        f"{name} (seat {seat.position + 1})" if names.count(name) > 1 else name
        for name, seat in zip(names, seats)
    ]


def game_rules(fmt: str) -> GameRules:
    rules = FORMATS[fmt]
    return GameRules(
        starting_life=rules.starting_life,
        commander_damage_limit=rules.commander_damage_limit,
    )


def build_agent(seat: Seat, seed: int):
    # Offset the seed per seat, so two random seats don't mirror each other.
    agent_seed = seed + seat.position
    if seat.agent == Seat.Agent.LLM:
        return LLMAgent(seat.model or None, seed=agent_seed)
    return RandomAgent(agent_seed)


def run_match(match_id) -> None:
    """Background job: plays ``match_id`` and records every event as it happens."""
    with job_log_context("match", match_id):
        match = Match.objects.get(pk=match_id)
        seats = list(match.seats.select_related("deck"))
        Match.objects.filter(pk=match_id).update(status=Match.Status.RUNNING)

        def persist(recorded):
            event = recorded.event
            MatchEvent.objects.create(
                match_id=match_id,
                seq=recorded.seq,
                round=recorded.round,
                turn=recorded.turn,
                seat_position=event.seat,
                kind=event.kind,
                text=event.text,
                payload=event.payload,
                reasoning=event.reasoning,
            )
            if recorded.round != match.round:
                match.round = recorded.round
                Match.objects.filter(pk=match_id).update(round=recorded.round)

        try:
            decks = []
            for seat, name in zip(seats, table_names(seats)):
                if seat.deck is None:
                    raise ValueError(f"The deck of seat {seat.position + 1} was deleted.")
                decks.append(deck_spec(name, seat.deck.cards))
            game = Game(
                decks,
                [build_agent(seat, match.seed) for seat in seats],
                game_rules(match.format),
                seed=match.seed,
                max_rounds=match.max_rounds,
                on_event=persist,
            )
            result = game.run()
        except Exception as exc:  # noqa: BLE001 - record any failure for the user.
            logger.exception("Match failed")
            Match.objects.filter(pk=match_id).update(
                status=Match.Status.FAILED, error=str(exc), finished_at=timezone.now()
            )
            return

        with transaction.atomic():
            by_position = {seat.position: seat for seat in seats}
            for outcome in result.seats:
                seat = by_position[outcome.seat]
                seat.life = outcome.life
                seat.eliminated_round = outcome.eliminated_round
                seat.save(update_fields=["life", "eliminated_round"])
            winner = by_position.get(result.winner_seat)
            Match.objects.filter(pk=match_id).update(
                status=Match.Status.FINISHED,
                round=result.rounds,
                winner=winner,
                end_reason=result.end_reason,
                finished_at=timezone.now(),
            )
        logger.info(
            "Match finished after %s rounds: %s",
            result.rounds,
            winner.deck_name if winner else "draw",
        )
