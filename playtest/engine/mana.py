"""Paying mana costs — never by enumerating payments.

Two layers:

* **Feasibility** is an exact integer max-flow: ``SRC -> source (cap = the
  mana it makes) -> color -> requirement (cap = the pips that color can pay)
  -> SINK``, with a ``GENERIC`` requirement every color feeds. A cost is
  payable iff the flow saturates every requirement. This handles sources
  making two mana (Sol Ring), any-color sources, hybrid and multi-pip costs
  correctly, where a greedy tapper does not.
* **Execution** picks *which* sources to tap: starting from every source, it
  drops the most valuable ones first (creatures, then rocks, then lands making
  many colors) as long as the rest still pays, then swaps a spent source for
  an unspent one whenever that leaves more distinct colors open. That second
  pass is what makes "casting a green spell doesn't tap your only blue
  source" true.

Sources with a cost (pay life, take damage, sacrifice it, or only while you
control a land type) are spent last, and only when the free ones cannot pay;
their cost is paid when the colors they supply need it.

Payment is automatic, not an agent decision: see the rules text.
"""

from collections import deque
from dataclasses import dataclass

from .cardrules import ManaAbility
from .effects import has
from .events import Event, EventKind
from .manacost import MANA_SYMBOLS, ManaCost
from .state import GameState, Permanent
from .zones import Zone, leave_battlefield

_INF = 10**6


@dataclass(frozen=True, slots=True)
class Source:
    """Something that can make mana right now: an untapped permanent or the pool."""

    # Sort key: cheaper-to-spend sources first.
    key: tuple
    # The permanent id, or None for one unit of floating mana.
    permanent_id: int | None
    colors: str
    amount: int
    pool_color: str = ""
    # The permanent's mana abilities usable right now.
    abilities: tuple[ManaAbility, ...] = ()


def _source_rank(perm: Permanent) -> int:
    if perm.spec.is_land:
        return 1
    if perm.spec.is_creature:
        return 3
    return 2


def can_tap_for_mana(state: GameState, perm: Permanent) -> bool:
    if perm.tapped or not perm.spec.mana_abilities:
        return False
    # A creature's {T} ability needs it to have been under control since the turn began.
    return not (perm.spec.is_creature and perm.sick and not has(state, perm, "haste"))


def usable_abilities(state: GameState, perm: Permanent) -> tuple[ManaAbility, ...]:
    """The mana abilities whose "activate only if you control …" condition holds."""
    lands = {
        subtype for p in state.battlefield if p.controller == perm.controller and p.spec.is_land for subtype in p.spec.subtypes
    }
    return tuple(a for a in perm.spec.mana_abilities if not a.needs or lands & set(a.needs))


def _cost_rank(abilities) -> int:
    if any(a.free for a in abilities):
        return 0
    return 2 if all(a.sacrifice for a in abilities) else 1


def sources(state: GameState, seat: int) -> list[Source]:
    """Every source of mana ``seat`` could use now, in spending order."""
    player = state.player(seat)
    found = []
    for color in MANA_SYMBOLS:
        for index in range(player.mana_pool.get(color, 0)):
            found.append(Source((0, 1, 0, index), None, color, 1, color))
    for perm in state.battlefield:
        if perm.controller == seat and can_tap_for_mana(state, perm):
            abilities = usable_abilities(state, perm)
            if not abilities:
                continue
            colors = "".join(c for c in MANA_SYMBOLS if any(c in a.colors for a in abilities))
            amount = max(a.amount for a in abilities)
            key = (1, _cost_rank(abilities), len(colors), _source_rank(perm), perm.id)
            found.append(Source(key, perm.id, colors, amount, abilities=abilities))
    found.sort(key=lambda s: s.key)
    return found


def available_mana(srcs: list[Source]) -> int:
    return sum(s.amount for s in srcs)


def available_colors(srcs: list[Source]) -> set[str]:
    return {c for s in srcs for c in s.colors}


def _max_flow(capacity: list[dict[int, int]], source: int, sink: int) -> int:
    """Edmonds–Karp over an adjacency map of residual capacities."""
    flow = 0
    while True:
        parent = {source: source}
        queue = deque([source])
        while queue and sink not in parent:
            node = queue.popleft()
            for nxt in sorted(capacity[node]):
                if capacity[node][nxt] > 0 and nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        if sink not in parent:
            return flow
        bottleneck = _INF
        node = sink
        while node != source:
            bottleneck = min(bottleneck, capacity[parent[node]][node])
            node = parent[node]
        node = sink
        while node != source:
            prev = parent[node]
            capacity[prev][node] -= bottleneck
            capacity[node].setdefault(prev, 0)
            capacity[node][prev] += bottleneck
            node = prev
        flow += bottleneck


def _flow(srcs: list[Source], cost: ManaCost):
    """Solves the payment flow; returns (paid in full, per-source colors used)."""
    needed = cost.generic + len(cost.pips)
    if needed == 0:
        return True, [{} for _ in srcs]
    if available_mana(srcs) < needed:
        return False, None
    requirements: dict[str, int] = {}
    for pip in cost.pips:
        requirements[pip] = requirements.get(pip, 0) + 1
    if cost.generic:
        requirements["*"] = cost.generic

    n = len(srcs)
    color_node = {c: n + 1 + i for i, c in enumerate(MANA_SYMBOLS)}
    req_names = sorted(requirements)
    req_node = {r: n + 1 + len(MANA_SYMBOLS) + i for i, r in enumerate(req_names)}
    sink = n + 1 + len(MANA_SYMBOLS) + len(req_names)
    capacity: list[dict[int, int]] = [{} for _ in range(sink + 1)]
    for i, src in enumerate(srcs, start=1):
        capacity[0][i] = src.amount
        for color in src.colors:
            capacity[i][color_node[color]] = src.amount
    for color, node in color_node.items():
        for req, rnode in req_node.items():
            if req == "*" or color in req:
                capacity[node][rnode] = _INF
    for req, rnode in req_node.items():
        capacity[rnode][sink] = requirements[req]
    original = [dict(edges) for edges in capacity]
    paid = _max_flow(capacity, 0, sink) == needed
    used = []
    for i, src in enumerate(srcs, start=1):
        colors = {}
        for color in src.colors:
            flow = original[i][color_node[color]] - capacity[i][color_node[color]]
            if flow > 0:
                colors[color] = flow
        used.append(colors)
    return paid, used


def can_pay(srcs: list[Source], cost: ManaCost) -> bool:
    """Whether ``srcs`` can pay ``cost`` (with ``{X}`` already turned into generic)."""
    return _flow(srcs, cost)[0]


def _distinct_colors(srcs) -> int:
    return len(available_colors(srcs))


def choose_payment(srcs: list[Source], cost: ManaCost) -> list[Source] | None:
    """The sources to spend on ``cost``, or None when it cannot be paid. Deterministic."""
    if not can_pay(srcs, cost):
        return None
    chosen = list(srcs)
    # Drop the most valuable sources first while the rest still pays.
    for src in sorted(srcs, key=lambda s: s.key, reverse=True):
        rest = [s for s in chosen if s is not src]
        if can_pay(rest, cost):
            chosen = rest
    # Keep more colors open: swap a spent source for an unspent one when that helps.
    improved = True
    while improved:
        improved = False
        unspent = [s for s in srcs if s not in chosen]
        for spent in sorted(chosen, key=lambda s: s.key, reverse=True):
            for spare in unspent:
                candidate = sorted([s for s in chosen if s is not spent] + [spare], key=lambda s: s.key)
                left = [s for s in srcs if s not in candidate]
                if _distinct_colors(left) > _distinct_colors(unspent) and can_pay(candidate, cost):
                    chosen = candidate
                    improved = True
                    break
            if improved:
                break
    return sorted(chosen, key=lambda s: s.key)


def _free_colors(src: Source) -> str:
    return "".join(c for c in src.colors if any(a.free and c in a.colors for a in src.abilities))


def _ability_for(abilities, colors: dict[str, int]) -> ManaAbility:
    """The cheapest ability that supplies ``colors``: free, then life, then a sacrifice."""
    total = sum(colors.values())
    fitting = [a for a in abilities if set(colors) <= set(a.colors) and a.amount >= total]
    order = lambda a: (not a.free, a.sacrifice, a.life, -a.amount)  # noqa: E731
    return min(fitting or abilities, key=order)


def pay(state: GameState, seat: int, cost: ManaCost) -> list[Event]:
    """Spends ``seat``'s mana on ``cost``; returns the events of any cost paid with it.

    Raises ``ValueError`` when the cost cannot be paid — callers only pay
    costs they checked with :func:`can_pay`.
    """
    srcs = sources(state, seat)
    chosen = choose_payment(srcs, cost)
    if chosen is None:
        raise ValueError(f"cannot pay {cost}")
    # Prefer the free colors of the chosen sources; costly ones only when needed.
    free_only = [
        Source(src.key, src.permanent_id, _free_colors(src), src.amount, src.pool_color, src.abilities)
        if src.permanent_id is not None and _free_colors(src)
        else src
        for src in chosen
    ]
    paid_free, used = _flow(free_only, cost)
    if not paid_free:
        _, used = _flow(chosen, cost)
    player = state.player(seat)
    events: list[Event] = []
    for src, colors in zip(chosen, used):
        if src.permanent_id is None:
            player.mana_pool[src.pool_color] -= 1
            if not player.mana_pool[src.pool_color]:
                del player.mana_pool[src.pool_color]
            continue
        perm = state.permanent(src.permanent_id)
        perm.tapped = True
        ability = _ability_for(src.abilities, colors)
        spent = sum(colors.values())
        if ability.free and ability.amount > spent and ability.colors:
            # A source making more than the cost still needs (Sol Ring for {1})
            # leaves the rest floating, in its first color, until the step ends.
            color = ability.colors[0]
            player.mana_pool[color] = player.mana_pool.get(color, 0) + ability.amount - spent
        if ability.life:
            player.life -= ability.life
            events.append(
                Event(
                    EventKind.EFFECT,
                    seat,
                    f"{player.name} pays {ability.life} life for {perm.name}'s mana ({player.life}).",
                    {"effect": "lose_life", "amount": ability.life, "life": player.life, "source": perm.name, "source_seat": seat},
                )
            )
        if ability.sacrifice:
            events.append(Event(EventKind.EFFECT, seat, f"{perm.name} is sacrificed for mana.", {"effect": "sacrifice", "card": perm.name}))
            events += leave_battlefield(state, perm, Zone.GRAVEYARD)
    return events
