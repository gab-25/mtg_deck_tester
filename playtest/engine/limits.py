"""Caps that keep every game finite.

The kernel runs in a background daemon thread: a game that never ends is a
match stuck in ``running`` forever. Every cap emits a structured event when it
bites and ends the thing it caps cleanly.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Limits:
    # The game is a draw after this many rounds (EndReason.TURN_LIMIT).
    max_rounds: int = 20
    # Agent decisions in one turn, every seat included; then the turn ends.
    max_decisions_per_turn: int = 150
    # No spell can be cast while the stack is this deep.
    max_stack_depth: int = 50
    # Activated abilities of one permanent in one turn (enforced once abilities exist, #28).
    max_activations_per_permanent_per_turn: int = 20
    # State-based action passes in one check before the check gives up.
    max_sba_iterations: int = 100
    # Declarations one player makes while declaring attackers or blockers.
    max_declaration_steps: int = 40
    # Jev's ``choice`` limit: a decision never offers more options than this.
    max_options_per_decision: int = 255
