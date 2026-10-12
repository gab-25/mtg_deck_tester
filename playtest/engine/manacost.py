"""Mana costs: parsing ``{2}{G}{G}`` into something the kernel can pay.

Shared by the card pre-pass (to read costs once) and the kernel (to pay them),
so a cost the kernel cannot read is rejected at compile time, not mid-game.
"""

import re
from dataclasses import dataclass

# The five colors plus colorless-specific mana ({C}).
COLORS = "WUBRG"
MANA_SYMBOLS = COLORS + "C"

_SYMBOL = re.compile(r"\{([^}]+)\}")


class ManaCostError(ValueError):
    """A mana cost the kernel cannot read."""


@dataclass(frozen=True, slots=True)
class ManaCost:
    """A cost as the kernel pays it.

    ``pips`` lists one entry per colored symbol, each the string of colors
    that may pay it (``"G"``, or ``"WG"`` for the hybrid ``{G/W}``). ``x`` is
    how many ``{X}`` the cost carries; their value is chosen at cast time.
    """

    generic: int = 0
    pips: tuple[str, ...] = ()
    x: int = 0

    @property
    def mana_value(self) -> int:
        return self.generic + len(self.pips)

    def total(self, x_value: int = 0) -> int:
        return self.generic + len(self.pips) + self.x * x_value

    def plus_generic(self, amount: int) -> "ManaCost":
        return ManaCost(self.generic + amount, self.pips, self.x)

    def with_x(self, x_value: int) -> "ManaCost":
        """The cost with every ``{X}`` replaced by ``x_value`` generic mana."""
        return ManaCost(self.generic + self.x * x_value, self.pips, 0)

    def __str__(self) -> str:
        parts = ["{X}"] * self.x
        if self.generic or not self.pips and not self.x:
            parts.append(f"{{{self.generic}}}")
        parts += [f"{{{'/'.join(p)}}}" for p in self.pips]
        return "".join(parts)


def parse_mana_cost(text: str) -> ManaCost:
    """Reads a Scryfall mana cost string; an empty string is a zero cost.

    Approximations, all of them stricter than the printed card (a cost is never
    cheaper here than on paper): Phyrexian ``{G/P}`` must be paid with green,
    ``{2/W}`` with white, and snow ``{S}`` with any mana.

    Raises :class:`ManaCostError` on anything else.
    """
    text = (text or "").strip()
    if not text:
        return ManaCost()
    if _SYMBOL.sub("", text).strip():
        raise ManaCostError(f"unreadable mana cost {text!r}")

    generic = 0
    pips: list[str] = []
    x = 0
    for raw in _SYMBOL.findall(text):
        symbol = raw.upper()
        if symbol.isdigit():
            generic += int(symbol)
        elif symbol == "X":
            x += 1
        elif symbol == "S":
            generic += 1
        elif symbol in MANA_SYMBOLS:
            pips.append(symbol)
        elif "/" in symbol:
            halves = symbol.split("/")
            colors = [h for h in halves if h in MANA_SYMBOLS]
            known = all(h in MANA_SYMBOLS or h in ("2", "P") for h in halves)
            if len(halves) != 2 or not colors or not known:
                raise ManaCostError(f"unreadable mana symbol {{{raw}}}")
            # {G/W} -> "WG" (WUBRG order); {G/P} and {2/W} -> the colored half only.
            pips.append("".join(sorted(set(colors), key=MANA_SYMBOLS.index)))
        else:
            raise ManaCostError(f"unreadable mana symbol {{{raw}}}")
    pips.sort(key=lambda p: (len(p), [MANA_SYMBOLS.index(c) for c in p]))
    return ManaCost(generic, tuple(pips), x)


def parse_mana_symbols(text: str) -> tuple[str, ...]:
    """Reads mana *produced*, e.g. ``"{G}{G}"`` -> ``("G", "G")``.

    Raises :class:`ManaCostError` unless every symbol is a single color or {C}.
    """
    symbols = _SYMBOL.findall(text or "")
    if not symbols or _SYMBOL.sub("", text).strip():
        raise ManaCostError(f"unreadable mana {text!r}")
    out = []
    for raw in symbols:
        if raw.upper() not in MANA_SYMBOLS:
            raise ManaCostError(f"unreadable mana symbol {{{raw}}}")
        out.append(raw.upper())
    return tuple(out)
