"""The kernel's guards: every cap ends cleanly, with an event, and never hangs.

A match runs in a daemon thread (``mtg_deck_tester.jobs.start_job``), where a
hang means a match stuck in "running" forever, so every loop in the kernel is
bounded by one of these.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    # The round cap: the game is a draw when it is reached.
    max_rounds: int = 20
    # Agent decisions in one turn, every seat included; past it every seat
    # passes for the rest of the turn.
    max_decisions_per_turn: int = 200
    max_stack_depth: int = 50
    max_activations_per_permanent_per_turn: int = 20
    max_sba_iterations: int = 100
    # Attacker / blocker declarations in one combat, per declaring player.
    max_declaration_steps: int = 40
    # Jev's limit on the options of one choice.
    max_options_per_decision: int = 255
    # Calls to a metered (model-backed) agent per seat, and the whole-match
    # wall clock; past either, metered seats are handed to their fallback.
    max_agent_calls: int = 400
    max_wall_clock_seconds: int = 300
    # Cast actions enumerate target combinations up to this many; above it the
    # cast commits and the targets are chosen one by one.
    max_enumerated_targets: int = 8
    max_x: int = 10
