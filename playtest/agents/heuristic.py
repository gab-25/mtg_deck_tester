"""A deterministic, boring heuristic agent.

It is an opponent you can pick for a seat, the Jev agent's fallback, and the
engine of every test — so it is kept simple: its bugs would look like engine
bugs. Ordered rules, first match wins, ties broken by name and id:

land → commander → removal on the best target → highest-MV creature → other
useful spell → counter an opponent's spell → profitable or lethal attacks, at
the opponent it pays most to attack → blocks to survive and favourable trades
→ pass.

Its evaluations are pure public helpers over the player view (combat outcome,
attack facts, lethal on board, block facts, removal value): Jev does no
arithmetic, so the Jev agent reuses them to annotate each option with facts.
"""

from ..engine.actions import Action, ActionKind
from .base import Decision

HARMFUL = {"DEAL_DAMAGE", "DESTROY", "EXILE", "RETURN_TO_HAND", "TAP", "LOSE_LIFE", "DISCARD_CARDS", "MILL", "COUNTER_SPELL"}
KILLING = {"DEAL_DAMAGE", "DESTROY", "EXILE", "RETURN_TO_HAND"}
KEYWORD_VALUE = {
    "flying": 2,
    "double strike": 3,
    "first strike": 1,
    "deathtouch": 2,
    "lifelink": 1,
    "trample": 1,
    "menace": 1,
    "hexproof": 1,
    "indestructible": 3,
    "vigilance": 1,
}

# -- reading the view ------------------------------------------------------------


def permanents(view: dict) -> dict[int, dict]:
    """Every permanent on the battlefield, by id."""
    found = {p["id"]: p for p in view["you"]["battlefield"]}
    for opponent in view["opponents"]:
        found.update({p["id"]: p for p in opponent["battlefield"]})
    return found


def _kw(perm: dict, keyword: str) -> bool:
    return keyword in perm.get("keywords", ())


def life_of(view: dict, seat: int) -> int:
    if seat == view["you"]["seat"]:
        return view["you"]["life"]
    return next(o["life"] for o in view["opponents"] if o["seat"] == seat)


def creatures_of(view: dict, seat: int) -> list[dict]:
    if seat == view["you"]["seat"]:
        perms = view["you"]["battlefield"]
    else:
        perms = next(o["battlefield"] for o in view["opponents"] if o["seat"] == seat)
    return [p for p in perms if "creature" in p["types"]]


# -- pure evaluations (reused by the Jev agent) ----------------------------------


def removal_value(perm: dict) -> int:
    """How much it is worth to get rid of a permanent."""
    if "creature" not in perm["types"]:
        return 1 if "land" in perm["types"] else 3
    value = perm.get("power", 0) + perm.get("toughness", 0)
    value += sum(KEYWORD_VALUE.get(k, 0) for k in perm.get("keywords", ()))
    value += 4 if perm.get("commander") else 0
    value += 2 if perm.get("effects") else 0
    return max(value, 1)


def _deals(source: dict, target: dict, damage_taken: int) -> bool:
    """Whether ``source``'s damage kills ``target`` (which already took ``damage_taken``)."""
    amount = source.get("power", 0)
    if amount <= 0 or _kw(target, "indestructible"):
        return False
    if _kw(source, "deathtouch"):
        return True
    return amount + damage_taken + target.get("damage", 0) >= target.get("toughness", 0)


def combat_outcome(attacker: dict, blocker: dict) -> tuple[bool, bool]:
    """``(attacker_dies, blocker_dies)`` when ``blocker`` blocks ``attacker`` alone."""
    first_a = _kw(attacker, "first strike") or _kw(attacker, "double strike")
    first_b = _kw(blocker, "first strike") or _kw(blocker, "double strike")
    a_dead = b_dead = False
    a_taken = b_taken = 0
    # First-strike damage step.
    if first_a or first_b:
        if first_a:
            b_dead = _deals(attacker, blocker, 0)
            b_taken += attacker.get("power", 0)
        if first_b:
            a_dead = _deals(blocker, attacker, 0)
            a_taken += blocker.get("power", 0)
    # Regular damage step: creatures that didn't deal first-strike damage, plus double strikers.
    a_deals = not a_dead and (not first_a or _kw(attacker, "double strike"))
    b_deals = not b_dead and (not first_b or _kw(blocker, "double strike"))
    new_b_dead = b_dead or (a_deals and _deals(attacker, blocker, b_taken if first_a else 0))
    new_a_dead = a_dead or (b_deals and _deals(blocker, attacker, a_taken if first_b else 0))
    return new_a_dead, new_b_dead


def can_block(blocker: dict, attacker: dict) -> bool:
    if "creature" not in blocker["types"] or blocker.get("tapped") or blocker.get("blocking") is not None:
        return False
    if _kw(attacker, "flying") and not (_kw(blocker, "flying") or _kw(blocker, "reach")):
        return False
    return True


def potential_blockers(view: dict, attacker: dict, defender: int) -> list[dict]:
    return [b for b in creatures_of(view, defender) if can_block(b, attacker)]


def attack_facts(view: dict, attacker: dict, defender: int) -> dict:
    """What attacking ``defender`` with ``attacker`` risks, computed in code."""
    blockers = potential_blockers(view, attacker, defender)
    killed_by, trades_with, eats = [], [], []
    for blocker in blockers:
        a_dies, b_dies = combat_outcome(attacker, blocker)
        if a_dies and b_dies:
            trades_with.append(blocker["name"])
        elif a_dies:
            killed_by.append(blocker["name"])
        elif b_dies:
            eats.append(blocker["name"])
    return {
        "power": attacker.get("power", 0),
        "defender_life": life_of(view, defender),
        "blockers": sorted(b["name"] for b in blockers),
        "killed_by": sorted(killed_by),
        "trades_with": sorted(trades_with),
        "kills_blockers": sorted(eats),
        "unblockable": not blockers,
        "safe": not killed_by and not trades_with,
    }


def possible_attackers(view: dict) -> list[dict]:
    return [
        p
        for p in creatures_of(view, view["you"]["seat"])
        if not p.get("tapped") and not p.get("summoning_sick") and not _kw(p, "defender")
    ]


def lethal_on_board(view: dict, defender: int, attackers: list[dict] | None = None) -> bool:
    """Whether ``attackers`` (default: everything that can attack) kill ``defender`` through any blocks."""
    attackers = sorted(
        attackers if attackers is not None else possible_attackers(view) + _declared(view, defender),
        key=lambda p: -p.get("power", 0),
    )
    blockers = [b for b in creatures_of(view, defender) if not b.get("tapped")]
    remaining = list(attackers)
    # Each blocker stops the biggest attacker it can block.
    for blocker in sorted(blockers, key=lambda b: -b.get("toughness", 0)):
        stoppable = next((a for a in remaining if can_block(blocker, a) and not _kw(a, "trample")), None)
        if stoppable is not None:
            remaining.remove(stoppable)
    return sum(a.get("power", 0) for a in remaining) >= life_of(view, defender)


def _declared(view: dict, defender: int) -> list[dict]:
    return [p for p in creatures_of(view, view["you"]["seat"]) if p.get("attacking") == defender]


def block_facts(attacker: dict, blocker: dict) -> dict:
    attacker_dies, blocker_dies = combat_outcome(attacker, blocker)
    return {"attacker_dies": attacker_dies, "blocker_dies": blocker_dies}


def incoming_damage(view: dict) -> int:
    """Combat damage coming at you from attackers nobody blocks yet."""
    me = view["you"]["seat"]
    perms = permanents(view)
    return sum(
        p.get("power", 0) for p in perms.values() if p.get("attacking") == me and not p.get("blocked_by")
    )


# -- what an action does -----------------------------------------------------------


def _card(view: dict, card_id: int) -> dict | None:
    for zone in (view["you"]["hand"], view["you"]["command_zone"]):
        for card in zone:
            if card["id"] == card_id:
                return card
    return None


def action_abilities(view: dict, action: Action) -> list[dict]:
    """The abilities an action puts on the stack, as the view describes them."""
    if action.kind in (ActionKind.CAST, ActionKind.CAST_COMMANDER):
        card = _card(view, action.card) or {}
        if "instant" not in card.get("types", ()) and "sorcery" not in card.get("types", ()):
            return []
        effects = card.get("effects", [])
        chosen = [e for e in effects if e["trigger"] == "SPELL" and e.get("mode") is None]
        if action.mode is not None and action.mode < len(effects):
            chosen.append(effects[action.mode])
        return chosen
    if action.kind == ActionKind.ACTIVATE:
        perm = permanents(view).get(action.permanent, {})
        effects = perm.get("effects", [])
        return [effects[action.ability]] if action.ability < len(effects) else []
    return []


def _owner(view: dict, ref: str) -> int | None:
    kind, value = ref[0], int(ref[1:])
    if kind == "p":
        return value
    if kind == "o":
        perm = permanents(view).get(value)
        return perm["controller"] if perm else None
    item = next((s for s in view["stack"] if s["id"] == value), None)
    return item["controller"] if item else None


def _op_value(view: dict, op: dict, ref: str | None, x: int) -> float:
    """The value of one op for the acting seat (positive is good for it)."""
    me = view["you"]["seat"]
    amount = op.get("amount")
    amount = x if amount == "X" else (amount or 0)
    refs = [ref] if ref is not None else _selector_refs(view, op.get("selector"))
    total = 0.0
    for r in refs:
        owner = _owner(view, r)
        if owner is None:
            continue
        sign = -1 if owner == me else 1
        perm = permanents(view).get(int(r[1:])) if r[0] == "o" else None
        name = op["op"]
        if name == "DEAL_DAMAGE":
            if perm is None:
                total += sign * amount * (3 if amount >= life_of(view, owner) else 1)
            elif _kw(perm, "indestructible"):
                total += 0
            elif amount + perm.get("damage", 0) >= perm.get("toughness", 0) or (amount and _kw(perm, "deathtouch")):
                total += sign * removal_value(perm)
        elif name in ("DESTROY", "EXILE"):
            if perm is not None and not (name == "DESTROY" and _kw(perm, "indestructible")):
                total += sign * removal_value(perm)
        elif name == "RETURN_TO_HAND" and perm is not None:
            total += sign * removal_value(perm) / 2
        elif name == "TAP" and perm is not None:
            total += sign * 0.5
        elif name == "LOSE_LIFE":
            total += sign * amount
        elif name == "DISCARD_CARDS":
            total += sign * 2 * amount
        elif name == "MILL":
            total += sign * 0.3 * amount
        elif name == "COUNTER_SPELL":
            total += sign * 5
        elif name == "GAIN_LIFE":
            total -= sign * 0.5 * amount
        elif name == "DRAW_CARDS":
            total -= sign * 3 * amount
        elif name == "UNTAP" and perm is not None:
            total -= sign * 0.3
        elif name == "PUT_COUNTERS" and perm is not None:
            if op.get("counter") == "+1/+1":
                total -= sign * 2 * amount
            elif amount >= perm.get("toughness", 0):
                total += sign * removal_value(perm)
            else:
                total += sign * amount
        elif name == "MODIFY_PT" and perm is not None:
            if perm.get("toughness", 0) + op.get("toughness", 0) <= 0:
                total += sign * removal_value(perm)
            else:
                total -= sign * 0.2 * (op.get("power", 0) + op.get("toughness", 0))
        elif name == "GRANT_KEYWORD" and perm is not None:
            total -= sign * 0.2
        elif name == "SACRIFICE":
            total += sign * 3 if r[0] == "p" else -removal_value(perm or {"types": ()})
    if op["op"] == "CREATE_TOKEN":
        total += (amount or 1) * ((op.get("power") or 0) + (op.get("toughness") or 0))
    if op["op"] == "ADD_MANA":
        total += 0.1
    return total


def _selector_refs(view: dict, selector: str | None) -> list[str]:
    me = view["you"]["seat"]
    opponents = [o["seat"] for o in view["opponents"]]
    perms = permanents(view).values()
    creatures = [p for p in perms if "creature" in p["types"]]
    match selector:
        case "YOU":
            return [f"p{me}"]
        case "EACH_OPPONENT":
            return [f"p{s}" for s in opponents]
        case "EACH_PLAYER":
            return [f"p{s}" for s in [me, *opponents]]
        case "EACH_CREATURE" | "EACH_OTHER_CREATURE":
            return [f"o{p['id']}" for p in creatures]
        case "EACH_CREATURE_YOU_CONTROL" | "EACH_OTHER_CREATURE_YOU_CONTROL":
            return [f"o{p['id']}" for p in creatures if p["controller"] == me]
        case "EACH_CREATURE_OPPONENTS_CONTROL":
            return [f"o{p['id']}" for p in creatures if p["controller"] != me]
    return []


def action_value(view: dict, action: Action) -> float:
    """What a cast or activation is worth to the acting seat, from its effects."""
    targets = dict(action.targets)
    total = 0.0
    for index, ability in enumerate(action_abilities(view, action)):
        for op in ability["ops"]:
            ref = None
            if op.get("selector", "").startswith("TARGET_") or op.get("selector") == "ANY_TARGET":
                ref = next((r for slot, r in targets.items() if slot.startswith(f"{index}:")), None)
                if ref is None:
                    # Targets chosen after casting: assume the best one is found.
                    total += 2
                    continue
            total += _op_value(view, op, ref, action.x or 0)
    return total


def is_removal(view: dict, action: Action) -> bool:
    me = view["you"]["seat"]
    for ref in dict(action.targets).values():
        if ref[0] == "o" and _owner(view, ref) not in (None, me):
            if any(op["op"] in KILLING or op["op"] in ("PUT_COUNTERS", "MODIFY_PT") for a in action_abilities(view, action) for op in a["ops"]):
                return action_value(view, action) >= 1
    return False


# -- the agent -------------------------------------------------------------------


def _tie_break(option: Action) -> tuple:
    return (
        option.name,
        option.card or option.permanent or 0,
        option.targets,
        option.defender if option.defender is not None else -1,
        option.attacker or 0,
        option.x or 0,
        option.ref or "",
    )


def _best(options, score):
    """The highest-scoring option with a positive score; ties by name, then ids."""
    scored = [(score(o), o) for o in options]
    positive = [(s, o) for s, o in scored if s > 0]
    if not positive:
        return None
    return min(positive, key=lambda so: (-so[0], _tie_break(so[1])))[1]


class HeuristicAgent:
    """Ordered rules over the player view; never calls the network."""

    def __init__(self, seed: int | None = None):
        # Deterministic by construction; the seed is accepted for symmetry with other agents.
        self.seed = seed

    def choose(self, view: dict, options: list[Action]) -> Decision:
        return Decision(self._pick(view, options))

    def _pick(self, view: dict, options: list[Action]) -> Action:
        kinds = {o.kind for o in options}
        if ActionKind.CHOOSE_TARGET in kinds:
            return self._target(view, options)
        if kinds & {ActionKind.DECLARE_ATTACKER, ActionKind.END_ATTACKERS}:
            return self._attack(view, options)
        if kinds & {ActionKind.DECLARE_BLOCKER, ActionKind.END_BLOCKERS, ActionKind.UNDO_BLOCKER}:
            return self._block(view, options)
        return self._priority(view, options)

    # -- priority --------------------------------------------------------

    def _priority(self, view: dict, options: list[Action]) -> Action:
        me = view["you"]["seat"]
        my_turn = view["active_seat"] == me
        main = my_turn and view["step"] in ("main1", "main2") and not view["stack"]
        by_kind = lambda kind: [o for o in options if o.kind == kind]  # noqa: E731
        casts = by_kind(ActionKind.CAST)

        if main:
            lands = by_kind(ActionKind.PLAY_LAND)
            if lands:
                return min(lands, key=lambda o: ("enters tapped" in o.label, o.name, o.card))
            commander = _best(by_kind(ActionKind.CAST_COMMANDER), lambda o: 1 + max(action_value(view, o), 0))
            if commander:
                return commander
        # Answer an opponent's spell on the stack.
        if view["stack"] and view["stack"][-1]["controller"] != me:
            counter = _best(
                [o for o in casts if any(r[0] == "s" for r in dict(o.targets).values())],
                lambda o: action_value(view, o),
            )
            if counter:
                return counter
        instant_window = main or (not my_turn and view["step"] == "end_step" and not view["stack"])
        if instant_window:
            removal = _best([o for o in casts if is_removal(view, o)], lambda o: action_value(view, o))
            if removal:
                return removal
        if main:
            creatures = [o for o in casts if "creature" in (_card(view, o.card) or {}).get("types", ())]
            if creatures:
                return min(creatures, key=lambda o: (-(_card(view, o.card) or {}).get("mana_value", 0), _tie_break(o)))
            permanents_ = [o for o in casts if not action_abilities(view, o) and "instant" not in (_card(view, o.card) or {}).get("types", ())]
            spells = [o for o in casts if action_abilities(view, o)]
            other = _best(spells, lambda o: action_value(view, o)) or _best(permanents_, lambda o: 1 + (_card(view, o.card) or {}).get("mana_value", 0))
            if other:
                return other
        if instant_window and (view["step"] == "main2" or not my_turn):
            activation = _best(by_kind(ActionKind.ACTIVATE), lambda o: action_value(view, o) - 0.5)
            if activation:
                return activation
        return options[0]

    # -- combat ------------------------------------------------------------

    def _attack(self, view: dict, options: list[Action]) -> Action:
        perms = permanents(view)
        declares = [o for o in options if o.kind == ActionKind.DECLARE_ATTACKER]
        lethal = {o.defender for o in declares if lethal_on_board(view, o.defender)}

        def score(option: Action) -> float:
            attacker = perms[option.permanent]
            if option.defender in lethal:
                return 100 - life_of(view, option.defender)
            facts = attack_facts(view, attacker, option.defender)
            if attacker.get("power", 0) <= 0 or facts["killed_by"]:
                return 0
            if not facts["safe"]:
                # An even trade is worth offering when we outnumber their blockers.
                their_blockers = [b for b in creatures_of(view, option.defender) if not b.get("tapped")]
                if len(possible_attackers(view)) + len(_declared(view, option.defender)) <= len(their_blockers):
                    return 0
                return 5 + attacker.get("power", 0) - life_of(view, option.defender) / 10
            # Prefer the opponent closest to dying, then the one with fewest blockers.
            return 10 + attacker.get("power", 0) - life_of(view, option.defender) / 10 - len(facts["blockers"]) / 100

        choice = _best(declares, score)
        if choice is None:
            return next((o for o in options if o.kind == ActionKind.END_ATTACKERS), options[0])
        return choice

    def _block(self, view: dict, options: list[Action]) -> Action:
        perms = permanents(view)
        my_life = view["you"]["life"]
        incoming = incoming_damage(view)
        in_danger = incoming >= my_life
        end = next((o for o in options if o.kind == ActionKind.END_BLOCKERS), None)
        declares = [o for o in options if o.kind == ActionKind.DECLARE_BLOCKER]

        if end is None:
            # A menace attacker has a lone blocker: add a second one, or take it back.
            menace = [o for o in declares if _kw(perms[o.attacker], "menace") and len(perms[o.attacker].get("blocked_by", [])) == 1]
            if menace:
                return min(menace, key=lambda o: (removal_value(perms[o.permanent]), o.permanent))
            undo = [o for o in options if o.kind == ActionKind.UNDO_BLOCKER]
            return undo[0] if undo else options[0]

        def score(option: Action) -> float:
            attacker, blocker = perms[option.attacker], perms[option.permanent]
            if attacker.get("blocked_by"):
                return 0
            if _kw(attacker, "menace"):
                # Two blockers needed: only worth it when this pair can follow through.
                spare = [o for o in declares if o.attacker == attacker["id"] and o.permanent != blocker["id"]]
                if not spare or not in_danger:
                    return 0
            attacker_dies, blocker_dies = combat_outcome(attacker, blocker)
            if not blocker_dies:
                return 6 + (4 if attacker_dies else 0)
            if attacker_dies and removal_value(blocker) <= removal_value(attacker):
                return 3 + removal_value(attacker) - removal_value(blocker)
            commander_threat = attacker.get("commander") and attacker.get("power", 0) > 0
            if in_danger or commander_threat and incoming >= my_life // 2:
                # Chump: the biggest attacker, with the least valuable blocker.
                return 1 + attacker.get("power", 0) / 10 - removal_value(blocker) / 100
            return 0

        choice = _best(declares, score)
        return choice or end

    # -- targets -----------------------------------------------------------

    def _target(self, view: dict, options: list[Action]) -> Action:
        me = view["you"]["seat"]
        purpose = view.get("choosing_target") or {"ops": []}
        harmful = any(op["op"] in HARMFUL or op["op"] == "SACRIFICE" for op in purpose["ops"]) or any(
            op["op"] in ("PUT_COUNTERS",) and op.get("counter") == "-1/-1" or op["op"] == "MODIFY_PT" and (op.get("power") or 0) < 0
            for op in purpose["ops"]
        )

        def score(option: Action) -> float:
            value = sum(_op_value(view, {**op, "selector": purpose.get("selector")}, option.ref, 0) for op in purpose["ops"])
            owner = _owner(view, option.ref)
            # Never aim a harmful effect at yourself, or a helpful one at an opponent.
            if harmful and owner == me or not harmful and owner != me:
                return value - 100
            return value

        return min(options, key=lambda o: (-score(o), _tie_break(o)))
