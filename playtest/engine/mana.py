"""Paying mana costs: never by enumerating payments.

*Feasibility* is an exact integer max-flow::

    SRC -> source_i (cap = amount) -> color_c (cap = inf) -> requirement -> SINK

where a requirement is a pip symbol (cap = its count) or GENERIC (cap = the
generic part). A cost is payable iff the max flow equals its total. Sol Ring
(two mana from one source), any-color sources, hybrid and multi-pip costs all
fall out of the graph; a greedy tapper gets them wrong.

*Execution* reads the assignment off the flow. Sources are offered to the flow
in a fixed order — fewest colors first, lands before rocks, creatures last,
the colors the player has most of first, then id — so the sources spent are the
ones least worth keeping. A post-pass then swaps a spent source for an unspent
one that can make the same mana whenever that leaves more distinct colors
available. Together they make "casting a green spell doesn't tap your only blue
source" true, and the same state always produces the same payment.

Payment is not an agent decision: the engine auto-taps.
"""

from collections import deque
from dataclasses import dataclass

from .manacost import ManaCost
from .state import PlayerState

COLOR_ORDER = "WUBRGC"
_INF = 10**9


@dataclass(frozen=True, slots=True)
class ManaSource:
    id: int  # the permanent's id
    name: str
    colors: tuple[str, ...]
    amount: int
    # 0 for lands, 1 for other noncreature sources, 2 for creatures.
    rank: int


def mana_sources(player: PlayerState) -> list[ManaSource]:
    """Every permanent ``player`` could tap for mana right now, in spending order."""
    sources = []
    for perm in player.battlefield:
        spec = perm.spec
        if not spec.mana_output or not perm.can_tap_for_attack_or_mana:
            continue
        if spec.kind == "land":
            rank = 0
        elif perm.is_creature:
            rank = 2
        else:
            rank = 1
        sources.append(ManaSource(perm.id, perm.name, spec.produced_mana, spec.mana_output, rank))
    abundance = {c: sum(s.amount for s in sources if c in s.colors) for c in COLOR_ORDER}
    return sorted(
        sources,
        key=lambda s: (
            len(s.colors),
            s.rank,
            # Spend the colors there are most of: their rarest color, most abundant first.
            -min(abundance[c] for c in s.colors),
            s.id,
        ),
    )


def available_mana(sources: list[ManaSource]) -> int:
    return sum(s.amount for s in sources)


def _satisfies(color: str, symbol: str) -> bool:
    """Whether one mana of ``color`` pays one ``symbol`` pip ("2/W" pips count as W here)."""
    if symbol == "C":
        return color == "C"
    if "/" in symbol:
        return color in symbol.split("/")
    return color == symbol


def _variants(cost: ManaCost, extra_generic: int):
    """``(generic, pips)`` ways to read the cost: each "{2/W}" is either W or 2 generic."""
    generic = cost.generic + extra_generic
    plain = [(s, n) for s, n in cost.pips if not s.startswith("2/")]
    twobrid = [(s, n) for s, n in cost.pips if s.startswith("2/")]
    total = sum(n for _, n in twobrid)
    # Paying a {2/W} pip with W costs less mana: try that first.
    for as_generic in range(total + 1):
        pips = list(plain)
        left = as_generic
        for symbol, count in twobrid:
            spent = min(left, count)
            left -= spent
            if count - spent:
                pips.append((symbol, count - spent))
        yield generic + 2 * as_generic, pips


class _Flow:
    """Edmonds–Karp on a tiny graph. Edges are explored in insertion order."""

    def __init__(self, nodes: int):
        self.adj: list[list[int]] = [[] for _ in range(nodes)]
        self.to: list[int] = []
        self.cap: list[int] = []

    def add(self, u: int, v: int, cap: int) -> int:
        self.adj[u].append(len(self.to))
        self.to.append(v)
        self.cap.append(cap)
        self.adj[v].append(len(self.to))
        self.to.append(u)
        self.cap.append(0)
        return len(self.to) - 2

    def flow_on(self, edge: int) -> int:
        # The reverse edge's capacity is the flow pushed through ``edge``.
        return self.cap[edge ^ 1]

    def maxflow(self, src: int, sink: int) -> int:
        total = 0
        while True:
            parent = [-1] * len(self.adj)
            parent[src] = -2
            queue = deque([src])
            while queue and parent[sink] == -1:
                u = queue.popleft()
                for e in self.adj[u]:
                    v = self.to[e]
                    if self.cap[e] > 0 and parent[v] == -1:
                        parent[v] = e
                        queue.append(v)
            if parent[sink] == -1:
                return total
            push = _INF
            v = sink
            while v != src:
                e = parent[v]
                push = min(push, self.cap[e])
                v = self.to[e ^ 1]
            v = sink
            while v != src:
                e = parent[v]
                self.cap[e] -= push
                self.cap[e ^ 1] += push
                v = self.to[e ^ 1]
            total += push


def _solve(sources: list[ManaSource], generic: int, pips: list[tuple[str, int]]):
    """The flow for one reading of a cost: ``(paid in full, mana used per source)``."""
    need = generic + sum(n for _, n in pips)
    n = len(sources)
    src, colors0 = 0, 1 + n
    req0 = colors0 + len(COLOR_ORDER)
    generic_node = req0 + len(pips)
    sink = generic_node + 1
    flow = _Flow(sink + 1)
    source_edges = [flow.add(src, 1 + i, s.amount) for i, s in enumerate(sources)]
    color_edges = {}
    for i, s in enumerate(sources):
        for c in s.colors:
            color_edges[(i, c)] = flow.add(1 + i, colors0 + COLOR_ORDER.index(c), _INF)
    for ci, color in enumerate(COLOR_ORDER):
        # Colored requirements first, so a color is spent on what only it can pay.
        for pi, (symbol, _count) in enumerate(pips):
            if _satisfies(color, symbol):
                flow.add(colors0 + ci, req0 + pi, _INF)
        flow.add(colors0 + ci, generic_node, _INF)
    for pi, (_symbol, count) in enumerate(pips):
        flow.add(req0 + pi, sink, count)
    flow.add(generic_node, sink, generic)
    paid = flow.maxflow(src, sink) == need
    used = [flow.flow_on(e) for e in source_edges]
    made = {key: flow.flow_on(e) for key, e in color_edges.items()}
    return paid, used, made


def can_pay(sources: list[ManaSource], cost: ManaCost, extra_generic: int = 0) -> bool:
    # The least mana it could take: every pip paid with one mana.
    if cost.generic + extra_generic + sum(n for _, n in cost.pips) > available_mana(sources):
        return False
    return any(_solve(sources, g, p)[0] for g, p in _variants(cost, extra_generic))


def _distinct_colors(sources) -> int:
    return len({c for s in sources for c in s.colors})


def payment(
    sources: list[ManaSource], cost: ManaCost, extra_generic: int = 0
) -> list[ManaSource] | None:
    """The sources to tap to pay ``cost`` (plus ``extra_generic``), or None if it can't be paid."""
    for generic, pips in _variants(cost, extra_generic):
        paid, used, made = _solve(sources, generic, pips)
        if paid:
            break
    else:
        return None

    spent = [i for i, amount in enumerate(used) if amount]
    unspent = [i for i, amount in enumerate(used) if not amount]
    # Swap a spent source for an unspent one that makes the same mana, when the
    # swap leaves more distinct colors available. Each swap strictly improves,
    # so this ends.
    improved = True
    while improved:
        improved = False
        for si in list(spent):
            colors_made = [c for c in COLOR_ORDER if made.get((si, c))]
            for ui in unspent:
                candidate = sources[ui]
                if candidate.amount < used[si] or any(c not in candidate.colors for c in colors_made):
                    continue
                before = _distinct_colors(sources[i] for i in unspent)
                after = _distinct_colors([sources[i] for i in unspent if i != ui] + [sources[si]])
                if after > before:
                    spent[spent.index(si)] = ui
                    unspent[unspent.index(ui)] = si
                    used[ui], used[si] = used[si], 0
                    for c in colors_made:
                        made[(ui, c)] = made.pop((si, c))
                    improved = True
                    break
            if improved:
                break
    return [sources[i] for i in sorted(spent)]
