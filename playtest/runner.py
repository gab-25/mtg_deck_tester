"""Runs a stored match through the engine and stores what happened.

Before the game, every seat's deck is compiled (the judge, through the
compiled-rules cache) and its coverage report is stored on the seat: during
the game there are zero judge calls, so a game with non-model agents replays
from its seed. The compiler never raises — a card it cannot reach or read is
unsupported — so a compile problem degrades a card, never the match.
"""

import logging

from django.db import transaction
from django.utils import timezone

from decks.cache import DbCardCache
from decks.formats import FORMATS
from mtg_deck_tester.logging_context import job_log_context

from . import judge
from .agents import typesafe
from .agents.heuristic import HeuristicAgent
from .agents.jev_agent import JevAgent
from .agents.llm_agent import LLMAgent
from .agents.random_agent import RandomAgent
from .engine.cards import CardSpec, DeckSpec
from .engine.coverage import coverage_report
from .engine.game import Game
from .engine.limits import Limits
from .engine.state import GameRules
from .models import Match, MatchEvent, Seat
from .summary import summarize_match

logger = logging.getLogger(__name__)


def card_spec(entry: judge.DeckEntry) -> CardSpec:
    """The engine's card for one compiled deck entry (vanilla when unsupported)."""
    compiled = entry.card
    if compiled.facts is None:
        # Unreadable card data: a blank that can never be cast.
        return CardSpec(compiled.name, ("sorcery",), program=None)
    program = compiled.card_program() if compiled.supported else None
    return CardSpec.from_facts(compiled.facts, program)


def deck_spec(name: str, entries: list[judge.DeckEntry], *, has_commander: bool = True) -> DeckSpec:
    """Expands a compiled deck into its library and, in Commander formats, its
    commander (and partner)."""
    commanders = []
    library = []
    for entry in entries:
        spec = card_spec(entry)
        if has_commander and entry.is_commander and len(commanders) < 2:
            commanders.append(spec)
            library.extend([spec] * (entry.quantity - 1))
        else:
            library.extend([spec] * entry.quantity)
    if has_commander and not commanders:
        raise ValueError(f"The deck {name!r} has no commander.")
    commander, partner = (commanders + [None, None])[:2]
    return DeckSpec(name=name, commander=commander, partner=partner, library=tuple(library))


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
        format_name=rules.label,
    )


def build_agent(seat: Seat, seed: int):
    # Offset the seed per seat, so two seats of one kind don't mirror each other.
    agent_seed = seed + seat.position
    if seat.agent == Seat.Agent.LLM:
        return LLMAgent(seat.model or None, seed=agent_seed)
    if seat.agent == Seat.Agent.JEV:
        return JevAgent(seat.model or typesafe.default_model(), seed=agent_seed)
    if seat.agent == Seat.Agent.RANDOM:
        return RandomAgent(agent_seed)
    return HeuristicAgent(agent_seed)


def compile_seat(seat: Seat, cache) -> list[judge.DeckEntry]:
    """The compiled cards of a seat's deck: a metagame deck, or the user's deck."""
    if seat.metagame_deck_id is not None:
        if seat.metagame_deck is None:
            raise ValueError(f"The metagame deck of seat {seat.position + 1} was replaced by a newer sync.")
        return judge.compile_deck(seat.metagame_deck.main_cards, cache)
    if seat.deck is None:
        raise ValueError(f"The deck of seat {seat.position + 1} was deleted.")
    # The sideboard never plays.
    return judge.compile_deck(seat.deck.main_cards, cache)


def run_match(match_id) -> None:
    """Background job: plays ``match_id`` and records every event as it happens."""
    with job_log_context("match", match_id):
        match = Match.objects.get(pk=match_id)
        seats = list(match.seats.select_related("deck", "metagame_deck"))
        Match.objects.filter(pk=match_id).update(status=Match.Status.RUNNING)

        def persist(recorded):
            event = recorded.event
            MatchEvent.objects.create(
                match_id=match_id,
                seq=recorded.seq,
                round=recorded.round,
                turn=recorded.turn,
                step=recorded.step,
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
            cache = DbCardCache()
            has_commander = FORMATS[match.format].has_commander
            decks, coverages = [], []
            for seat, name in zip(seats, table_names(seats)):
                entries = compile_seat(seat, cache)
                report = coverage_report([entry.coverage() for entry in entries])
                Seat.objects.filter(pk=seat.pk).update(coverage=report)
                coverages.append(report)
                decks.append(deck_spec(name, entries, has_commander=has_commander))
            game = Game(
                decks,
                [build_agent(seat, match.seed) for seat in seats],
                game_rules(match.format),
                seed=match.seed,
                limits=Limits(max_rounds=match.max_rounds),
                on_event=persist,
            )
            result = game.run()
            summary = summarize_match(result.events, coverages, result, has_commander=has_commander)
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
                summary=summary,
                finished_at=timezone.now(),
            )
        logger.info(
            "Match finished after %s rounds: %s",
            result.rounds,
            winner.deck_name if winner else "draw",
        )
