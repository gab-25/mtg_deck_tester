"""An agent that asks Jev which legal action to take.

One Jev request per decision: the ``state`` is the seat's view filtered to
what a decision needs, and the single ``choice`` question's options *are* the
legal actions — keyed by a stable id, described by their label plus facts
computed in code with the heuristic agent's helpers ("lethal if unblocked",
"dies to: X", "kills Y"), because Jev does not do arithmetic. The options are
shuffled with the agent's seeded RNG before every request and mapped back by
key: Jev leans toward the first option, and the first legal action is always
``Pass``.

The answer's confidence and distribution are kept on the decision and shown in
the log in place of a written reason. Below ``JEV_MIN_CONFIDENCE``, or when
Jev cannot be reached, the heuristic agent decides instead — never at random.
"""

import json
import logging
import os
import random

from ..engine.actions import Action, ActionKind
from . import heuristic, typesafe
from .base import Decision
from .heuristic import HeuristicAgent

logger = logging.getLogger(__name__)

# Default confidence under which the heuristic agent decides instead; override
# with JEV_MIN_CONFIDENCE. Jev's confidence measures how peaked its distribution
# is, not the chosen option's probability. Measured on 4 benchmark games (222
# decisions, jev-1.13, 2026-10-12): median confidence ~0.4, and below 0.2 the
# top options are near-ties; 0.2 hands over ~20% of decisions, 0.5 would hand
# over almost half.
DEFAULT_MIN_CONFIDENCE = 0.2
# How many of Jev's top actions a decision keeps for the log.
TOP_PROBABILITIES = 3

# A condensed form of the rules text (engine/rules.py), limited to what matters for choosing.
INSTRUCTIONS = (
    "You play seat {seat} ({deck}) in a game of Magic: The Gathering ({format}). "
    "It is {active}'s turn, {step} step. Your goal is to win: reduce every opponent to 0 life "
    "while staying alive. The state lists life totals, your hand and every battlefield. "
    "Mana is paid automatically; creatures that arrived this turn cannot attack unless they have "
    "haste; attackers and blockers are declared one at a time. Each option is a legal action, "
    "with facts already computed for it. Which action is best right now?"
)


def default_min_confidence() -> float:
    try:
        return float(os.environ.get("JEV_MIN_CONFIDENCE") or DEFAULT_MIN_CONFIDENCE)
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE


# -- the state Jev sees ----------------------------------------------------------


def _slim_card(card: dict) -> dict:
    keep = {k: card[k] for k in ("name", "types", "cost", "power", "toughness", "keywords", "commander") if k in card}
    if card.get("effects"):
        keep["does"] = [e["text"] for e in card["effects"]]
    if card.get("unsupported"):
        keep["does"] = ["nothing (unsupported card)"]
    return keep


def _slim_permanent(perm: dict) -> dict:
    keep = {
        k: perm[k]
        for k in ("name", "types", "power", "toughness", "keywords", "tapped", "summoning_sick", "damage", "counters", "token", "commander")
        if k in perm
    }
    if "attacking" in perm:
        keep["attacking_seat"] = perm["attacking"] + 1
    if perm.get("effects"):
        keep["does"] = [e["text"] for e in perm["effects"]]
    return keep


def jev_state(view: dict) -> str:
    """The seat's view filtered to what a decision needs: no ids, no history."""
    you = view["you"]
    state = {
        "you": {
            "seat": you["seat"] + 1,
            "life": you["life"],
            "mana_available": you["mana_available"],
            "hand": [_slim_card(c) for c in you["hand"]],
            "battlefield": [_slim_permanent(p) for p in you["battlefield"]],
            "commander_in_command_zone": [c["name"] for c in you["command_zone"]],
            "commander_damage_taken": you["commander_damage_taken"],
        },
        "opponents": [
            {
                "seat": o["seat"] + 1,
                "life": o["life"],
                "cards_in_hand": o["hand_count"],
                "battlefield": [_slim_permanent(p) for p in o["battlefield"]],
            }
            for o in view["opponents"]
        ],
        "stack": [{"name": s["name"], "seat": s["controller"] + 1, "targets": s["targets"]} for s in view["stack"]],
    }
    return json.dumps(state, separators=(",", ":"))


# -- what each option means, computed in code ------------------------------------


def option_facts(view: dict, action: Action) -> list[str]:
    """Facts about an action that Jev could not compute itself."""
    perms = heuristic.permanents(view)
    facts: list[str] = []
    if action.kind == ActionKind.DECLARE_ATTACKER:
        attacker = perms.get(action.permanent, {})
        info = heuristic.attack_facts(view, attacker, action.defender)
        if info["power"] >= info["defender_life"]:
            facts.append("lethal if unblocked")
        facts.append(f"can be blocked by: {', '.join(info['blockers'])}" if info["blockers"] else "no creature can block it")
        if info["killed_by"]:
            facts.append(f"dies to: {', '.join(info['killed_by'])}")
        if info["trades_with"]:
            facts.append(f"trades with: {', '.join(info['trades_with'])}")
        if info["kills_blockers"]:
            facts.append(f"kills any of: {', '.join(info['kills_blockers'])} if blocked")
        untapped = [p for p in heuristic.possible_attackers(view) if p["id"] != action.permanent and "vigilance" not in p.get("keywords", ())]
        facts.append(f"leaves you with {len(untapped)} untapped creature(s)")
    elif action.kind == ActionKind.DECLARE_BLOCKER:
        attacker, blocker = perms.get(action.attacker, {}), perms.get(action.permanent, {})
        outcome = heuristic.block_facts(attacker, blocker)
        facts.append("your blocker dies" if outcome["blocker_dies"] else "your blocker survives")
        facts.append("kills the attacker" if outcome["attacker_dies"] else "the attacker survives")
        incoming = heuristic.incoming_damage(view) - attacker.get("power", 0)
        facts.append(f"you would then take {max(incoming, 0)} combat damage (life {view['you']['life']})")
    elif action.kind == ActionKind.END_BLOCKERS:
        incoming = heuristic.incoming_damage(view)
        life = view["you"]["life"]
        facts.append(f"you take {incoming} combat damage (life {life} -> {life - incoming})")
        if incoming >= life:
            facts.append("this is lethal for you")
    elif action.kind == ActionKind.END_ATTACKERS:
        attacking = [p for p in heuristic.creatures_of(view, view["you"]["seat"]) if "attacking" in p]
        facts.append(f"{len(attacking)} creature(s) attacking")
    elif action.kind in (ActionKind.CAST, ActionKind.CAST_COMMANDER, ActionKind.ACTIVATE):
        for ability in heuristic.action_abilities(view, action):
            facts.append(f"does: {ability['text']}")
        for _, ref in action.targets:
            if ref[0] == "o":
                perm = perms.get(int(ref[1:]))
                if perm is None:
                    continue
                value = heuristic.action_value(view, action)
                if value >= heuristic.removal_value(perm) and perm["controller"] != view["you"]["seat"]:
                    facts.append(f"kills {perm['name']}")
            elif ref[0] == "p":
                seat = int(ref[1:])
                facts.append(f"aimed at seat {seat + 1} ({heuristic.life_of(view, seat)} life)")
    elif action.kind == ActionKind.PASS:
        facts.append(f"keep {view['you']['mana_available']} mana untapped")
    elif action.kind == ActionKind.CHOOSE_TARGET and action.ref and action.ref[0] == "o":
        perm = perms.get(int(action.ref[1:]))
        if perm is not None:
            facts.append("yours" if perm["controller"] == view["you"]["seat"] else "an opponent's")
    return facts


def describe_option(view: dict, action: Action) -> str:
    facts = option_facts(view, action)
    return action.label + (" — " + "; ".join(facts) if facts else "")


# -- the agent -------------------------------------------------------------------


class JevAgent:
    """Plays through Jev; never blocks the game and never falls back to random."""

    # Model-backed: the game meters its calls and wall clock (see Limits).
    metered = True

    def __init__(self, model: str | None = None, *, seed: int | None = None, ask=None, min_confidence: float | None = None):
        self.model = model or typesafe.default_model()
        self.rng = random.Random(f"{seed}:jev")
        self.ask = ask or typesafe.system_one
        self.min_confidence = default_min_confidence() if min_confidence is None else min_confidence
        self.fallback = HeuristicAgent(seed)

    def choose(self, view: dict, options: list[Action]) -> Decision:
        if len(options) > typesafe.MAX_CHOICE_OPTIONS:
            raise ValueError(f"{len(options)} options: the kernel caps a decision at {typesafe.MAX_CHOICE_OPTIONS}")
        keys = [f"a{index}" for index in range(len(options))]
        by_key = dict(zip(keys, options))
        shuffled = list(keys)
        self.rng.shuffle(shuffled)
        criteria = {key: describe_option(view, by_key[key]) for key in shuffled}
        me = view["you"]
        active = "your" if view["active_seat"] == me["seat"] else f"seat {view['active_seat'] + 1}"
        instructions = INSTRUCTIONS.format(
            seat=me["seat"] + 1,
            deck=me["deck"],
            format=view.get("format", "Commander"),
            active=active,
            step=view["step"].replace("_", " "),
        )
        try:
            response = self.ask(jev_state(view), {"action": typesafe.choice(instructions, criteria)}, model=self.model)
            picked, confidence, probabilities = typesafe.read_choice(response["answers"].get("action"), by_key)
        except (typesafe.TypeSafeError, KeyError, TypeError) as exc:
            logger.warning("Jev agent (%s) fell back to the heuristic agent: %s", self.model, exc)
            return self._fallback(view, options, f"Jev unavailable: {exc}", "error")
        top = tuple(
            (by_key[key].label, p)
            for key, p in sorted(probabilities.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_PROBABILITIES]
            if key in by_key
        )
        return self.gate(view, options, by_key[picked], confidence, top, response.get("model", self.model))

    def gate(self, view, options, action: Action, confidence: float, top, model: str) -> Decision:
        """The confidence gate: the one place an uncertain decision is handed over (#44)."""
        if confidence < self.min_confidence:
            return self._fallback(
                view,
                options,
                f"low confidence {confidence:.2f} (below {self.min_confidence:.2f})",
                "low_confidence",
                confidence=confidence,
                probabilities=top,
                model=model,
            )
        return Decision(action, confidence=confidence, probabilities=top, model=model)

    def _fallback(self, view, options, reason: str, kind: str, **extra) -> Decision:
        action = self.fallback.choose(view, options).action
        return Decision(action, fallback_reason=reason, fallback_agent="heuristic", fallback_kind=kind, **extra)
