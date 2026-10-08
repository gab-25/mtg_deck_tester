"""A boring, deterministic agent: the opponent, the fallback, and the engine of every test.

Ordered rules, the first that matches wins; ties go to the option listed first,
which ``legal_actions`` sorts by card name and id:

1. play a land;
2. cast the commander;
3. cast removal at the best target (card effects, #28);
4. cast the creature with the highest mana value (the highest X);
5. cast any other spell;
6. attack: everything at an opponent it is lethal on, otherwise each creature
   at the opponent it pays most to attack, when no blocker eats it for free;
7. block: kill an attacker and survive, chump when the damage would kill us,
   trade when the blocker is worth no more than the attacker;
8. pass.

Spells are cast only in its own main phases with an empty stack. Its
evaluations are pure public helpers over the player view, so the Jev agent
(#29) can annotate each option with the same computed facts.
"""

from ..engine.actions import CLOSING_KINDS, Action, ActionKind
from .base import Decision

_EVASION = ("flying", "menace", "trample", "deathtouch", "lifelink", "first strike", "double strike")
_MAIN_STEPS = ("main1", "main2")


# -- pure helpers over the view ------------------------------------------------


def permanents_by_id(view: dict) -> dict[int, dict]:
    """Every permanent on the battlefield, each with its controller's ``seat``."""
    found = {}
    for side in [view["you"], *view["opponents"]]:
        for perm in side["battlefield"]:
            found[perm["id"]] = {**perm, "seat": side["seat"]}
    return found


def _has(perm: dict, keyword: str) -> bool:
    return keyword in perm.get("keywords", ())


def creature_value(perm: dict) -> int:
    """What a creature is worth, roughly: power counts double, evasion adds up."""
    bonus = sum(2 for k in _EVASION if _has(perm, k))
    return 2 * perm.get("power", 0) + perm.get("toughness", 0) + bonus


def removal_target_value(perm: dict) -> int:
    """How much it pays to remove ``perm``: creatures by value, the rest by mana value."""
    if perm.get("kind") == "creature":
        return creature_value(perm) + (10 if perm.get("commander") else 0)
    return perm.get("mana_value", 0)


def can_block(blocker: dict, attacker: dict) -> bool:
    if _has(attacker, "flying"):
        return _has(blocker, "flying") or _has(blocker, "reach")
    return True


def is_ready_attacker(perm: dict) -> bool:
    return (
        perm.get("kind") == "creature"
        and not perm.get("tapped")
        and not perm.get("summoning_sick")
        and not _has(perm, "defender")
    )


def potential_blockers(view: dict, seat: int) -> list[dict]:
    side = next(s for s in [view["you"], *view["opponents"]] if s["seat"] == seat)
    return [p for p in side["battlefield"] if p.get("kind") == "creature" and not p.get("tapped")]


def block_outcome(blocker: dict, attacker: dict) -> tuple[bool, bool]:
    """``(attacker dies, blocker dies)`` if ``blocker`` alone blocks ``attacker``."""

    def kills(source, target):
        left = target.get("toughness", 0) - target.get("damage", 0)
        if _has(target, "indestructible") or source.get("power", 0) <= 0:
            return False
        return _has(source, "deathtouch") or source["power"] >= left

    first = lambda p: _has(p, "first strike") or _has(p, "double strike")
    attacker_dies, blocker_dies = kills(blocker, attacker), kills(attacker, blocker)
    if first(attacker) and not first(blocker) and blocker_dies:
        attacker_dies = False
    if first(blocker) and not first(attacker) and attacker_dies:
        blocker_dies = False
    return attacker_dies, blocker_dies


def _opponent(view: dict, seat: int) -> dict:
    return next(o for o in view["opponents"] if o["seat"] == seat)


def _damage_to_lose(view: dict, seat: int, commander: bool) -> int:
    """Damage that eliminates ``seat``: life, or what is left of commander damage."""
    opponent = _opponent(view, seat)
    life = max(opponent["life"], 1)
    limit = view.get("commander_damage_limit")
    if commander and limit:
        return min(life, max(limit - opponent["commander_damage_taken_from_you"], 1))
    return life


def attack_value(view: dict, attacker: dict, defender_seat: int) -> int:
    """How much attacking ``defender_seat`` with ``attacker`` pays; 0 when a blocker eats it for free."""
    able = [b for b in potential_blockers(view, defender_seat) if can_block(b, attacker)]
    if _has(attacker, "menace") and len(able) < 2:
        able = []
    for blocker in able:
        attacker_dies, blocker_dies = block_outcome(blocker, attacker)
        if attacker_dies and not blocker_dies:
            return 0
    power = attacker.get("power", 0)
    if power <= 0:
        return 0
    return 1 + power * 100 // _damage_to_lose(view, defender_seat, bool(attacker.get("commander")))


def lethal_on_board(view: dict, defender_seat: int) -> bool:
    """Whether our ready creatures kill ``defender_seat`` even if they block as well as they can."""
    attackers = [
        p
        for p in view["you"]["battlefield"]
        if p.get("attacking_seat") == defender_seat or ("attacking_seat" not in p and is_ready_attacker(p))
    ]
    attackers.sort(key=lambda p: -p.get("power", 0))
    unblocked = list(attackers)
    for blocker in sorted(potential_blockers(view, defender_seat), key=lambda p: p["id"]):
        target = next((a for a in unblocked if can_block(blocker, a)), None)
        if target is not None:
            unblocked.remove(target)
    damage = sum(max(a.get("power", 0), 0) for a in unblocked)
    return bool(attackers) and damage >= _opponent(view, defender_seat)["life"]


def incoming_damage(view: dict) -> int:
    """Combat damage coming at us from attackers no one blocks yet."""
    me = view["you"]
    blocked = {p.get("blocking_id") for p in me["battlefield"]}
    perms = permanents_by_id(view)
    return sum(
        max(p.get("power", 0), 0)
        for p in perms.values()
        if p.get("attacking_seat") == me["seat"] and p["id"] not in blocked
    )


# -- the agent -----------------------------------------------------------------


def _best(options: list[Action], score) -> Action | None:
    """The option with the highest positive score; the first listed wins ties."""
    best, best_score = None, 0
    for option in options:
        value = score(option)
        if value > best_score:
            best, best_score = option, value
    return best


class HeuristicAgent:
    def __init__(self, seed: int | None = None):
        # Deterministic: the seed is accepted for symmetry with the other agents.
        self.seed = seed

    def choose(self, view: dict, options: list[Action]) -> Decision:
        return Decision(self._choose(view, options))

    def _choose(self, view: dict, options: list[Action]) -> Action:
        of = lambda kind: [o for o in options if o.kind == kind]
        my_main = view["your_turn"] and view["step"] in _MAIN_STEPS and not view["stack"]

        if of(ActionKind.PLAY_LAND):
            return of(ActionKind.PLAY_LAND)[0]
        if my_main:
            choice = self._cast(view, of(ActionKind.CAST_COMMANDER), of(ActionKind.CAST_SPELL))
            if choice is not None:
                return choice
        if of(ActionKind.DECLARE_ATTACKER) or ActionKind.END_ATTACKERS in {o.kind for o in options}:
            return self._attack(view, options)
        if of(ActionKind.DECLARE_BLOCKER) or of(ActionKind.UNDO_BLOCKER):
            return self._block(view, options)
        return options[0]

    def _cast(self, view, commander: list[Action], spells: list[Action]) -> Action | None:
        if commander:
            return max(commander, key=lambda o: o.x or 0)
        perms = permanents_by_id(view)
        targeted = [o for o in spells if o.target_uid in perms]
        if targeted:
            return _best(targeted, lambda o: removal_target_value(perms[o.target_uid]))
        hand = {c["id"]: c for c in view["you"]["hand"]}
        creatures = [o for o in spells if hand.get(o.card_uid, {}).get("kind") == "creature"]
        for group in (creatures, spells):
            if group:
                # ``max`` keeps the first of equals: ties go to the option listed first.
                return max(group, key=lambda o: hand[o.card_uid]["mana_value"] + (o.x or 0))
        return None

    def _attack(self, view, options: list[Action]) -> Action:
        declare = [o for o in options if o.kind == ActionKind.DECLARE_ATTACKER]
        perms = permanents_by_id(view)
        for opponent in view["opponents"]:
            if lethal_on_board(view, opponent["seat"]):
                lethal = [o for o in declare if o.target_seat == opponent["seat"]]
                if lethal:
                    return lethal[0]
        best = _best(declare, lambda o: attack_value(view, perms[o.card_uid], o.target_seat))
        return best or options[0]

    def _block(self, view, options: list[Action]) -> Action:
        perms = permanents_by_id(view)
        blocked = {p.get("blocking_id") for p in view["you"]["battlefield"]}
        pairs = [
            (o, perms[o.card_uid], perms[o.target_uid])
            for o in options
            if o.kind == ActionKind.DECLARE_BLOCKER
            and o.target_uid not in blocked
            and not _has(perms[o.target_uid], "menace")
        ]
        good = [(o, a) for o, b, a in pairs if block_outcome(b, a) == (True, False)]
        if good:
            return _best([o for o, _ in good], lambda o: 1 + creature_value(perms[o.target_uid]))
        if pairs and incoming_damage(view) >= view["you"]["life"]:
            # Chump the biggest attacker with the cheapest blocker.
            return max(
                (o for o, _, _ in pairs),
                key=lambda o: (perms[o.target_uid].get("power", 0), -creature_value(perms[o.card_uid])),
            )
        trades = [
            o for o, b, a in pairs if block_outcome(b, a) == (True, True) and creature_value(b) <= creature_value(a)
        ]
        if trades:
            return trades[0]
        if options[0].kind in CLOSING_KINDS:
            return options[0]
        # Menace withheld the end of blocks: take back the lone block.
        return next(o for o in options if o.kind == ActionKind.UNDO_BLOCKER)
