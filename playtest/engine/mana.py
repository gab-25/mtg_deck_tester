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

Payment is automatic, not an agent decision: see the rules text.
"""

from collections import deque
from dataclasses import dataclass

from .effects import has
from .manacost import MANA_SYMBOLS, ManaCost
from .state import GameState, Permanent

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


def _source_rank(perm: Permanent) -> int:
    if perm.spec.is_land:
        return 1
    if perm.spec.is_creature:
        return 3
    return 2


def can_tap_for_mana(state: GameState, perm: Permanent) -> bool:
    if perm.tapped or not perm.spec.is_mana_source:
        return False
    # A creature's {T} ability needs it to have been under control since the turn began.
    return not (perm.spec.is_creature and perm.sick and not has(state, perm, "haste"))


def sources(state: GameState, seat: int) -> list[Source]:
    """Every source of mana ``seat`` could use now, in spending order."""
    player = state.player(seat)
    found = []
    for color in MANA_SYMBOLS:
        for index in range(player.mana_pool.get(color, 0)):
            found.append(Source((0, 1, 0, index), None, color, 1, color))
    for perm in state.battlefield:
        if perm.controller == seat and can_tap_for_mana(state, perm):
            colors = "".join(perm.spec.produced_mana)
            found.append(Source((1, len(colors), _source_rank(perm), perm.id), perm.id, colors, perm.spec.mana_amount))
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


def can_pay(srcs: list[Source], cost: ManaCost) -> bool:
    """Whether ``srcs`` can pay ``cost`` (with ``{X}`` already turned into generic)."""
    needed = cost.generic + len(cost.pips)
    if needed == 0:
        return True
    if available_mana(srcs) < needed:
        return False
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
    return _max_flow(capacity, 0, sink) == needed


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


def pay(state: GameState, seat: int, cost: ManaCost) -> list[str]:
    """Spends ``seat``'s mana on ``cost``; returns the names of what was tapped.

    Raises ``ValueError`` when the cost cannot be paid — callers only pay
    costs they checked with :func:`can_pay`.
    """
    srcs = sources(state, seat)
    chosen = choose_payment(srcs, cost)
    if chosen is None:
        raise ValueError(f"cannot pay {cost}")
    player = state.player(seat)
    tapped = []
    # A source making more than the cost still needs (Sol Ring for {1}) leaves
    # the rest floating, in its first color, until the step ends.
    excess = sum(s.amount for s in chosen) - cost.total()
    spare = next((s for s in reversed(chosen) if s.amount > 1), None)
    if excess > 0 and spare is not None:
        color = spare.colors[0]
        player.mana_pool[color] = player.mana_pool.get(color, 0) + excess
    for src in chosen:
        if src.permanent_id is None:
            player.mana_pool[src.pool_color] -= 1
            if not player.mana_pool[src.pool_color]:
                del player.mana_pool[src.pool_color]
        else:
            perm = state.permanent(src.permanent_id)
            perm.tapped = True
            tapped.append(perm.name)
    return tapped
