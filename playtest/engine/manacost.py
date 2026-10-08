"""Mana costs as the kernel reads them: ``{2}{G}{G}`` -> 2 generic, two green pips.

The single parser for printed costs: the rules compiler validates activated
abilities with it, and the kernel pays with it.
"""

import re
from dataclasses import dataclass

COLORS = ("W", "U", "B", "R", "G")

_SYMBOL = re.compile(r"\{([^{}]+)\}")
# "{W/U}" (either color) and "{2/W}" (two generic or one white).
_HYBRID = re.compile(r"[WUBRG]/[WUBRG]|2/[WUBRG]")


class ManaCostError(ValueError):
    """A cost the kernel can't pay (Phyrexian, snow, half mana, malformed)."""


@dataclass(frozen=True, slots=True)
class ManaCost:
    generic: int = 0
    # Colored, colorless and hybrid pips as ``(symbol, count)``, sorted by symbol.
    pips: tuple[tuple[str, int], ...] = ()
    # Number of {X} in the cost; X is chosen when casting.
    x: int = 0

    @property
    def mana_value(self) -> int:
        # A "{2/W}" pip counts 2 towards mana value, every other pip 1; X counts 0.
        return self.generic + sum(2 * n if s.startswith("2/") else n for s, n in self.pips)


def parse_mana_cost(text: str) -> ManaCost:
    """Reads a printed cost; raises :class:`ManaCostError` on anything else."""
    compact = text.replace(" ", "")
    if not compact:
        return ManaCost()
    symbols = _SYMBOL.findall(compact)
    if "".join(f"{{{s}}}" for s in symbols) != compact:
        raise ManaCostError(f"unreadable mana cost {text!r}")

    generic = 0
    x = 0
    pips: dict[str, int] = {}
    for raw in symbols:
        symbol = raw.upper()
        if symbol.isdigit():
            generic += int(symbol)
        elif symbol == "X":
            x += 1
        elif symbol in COLORS or symbol == "C" or _HYBRID.fullmatch(symbol):
            pips[symbol] = pips.get(symbol, 0) + 1
        else:
            raise ManaCostError(f"unsupported mana symbol {{{raw}}}")
    return ManaCost(generic, tuple(sorted(pips.items())), x)
