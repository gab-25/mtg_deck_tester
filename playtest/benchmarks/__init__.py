"""Bundled benchmark decks: opponents that compile at 100% coverage.

Three 100-card decks for the Commander formats and three 60-card decks for
the constructed ones. Each deck is a decklist (``<key>.txt``) plus its card facts and compiled
programs (``<key>.rules.json``), committed so a benchmark costs no Scryfall
call, no judge call and no network — in tests and in production alike.
``manage.py build_benchmarks`` regenerates the ``.rules.json`` files.

Bundling real precons instead would cripple the opponent with the same
coverage gaps as the deck under test, and measure noise against noise.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from decks.formats import COMMANDER_FAMILY, CONSTRUCTED_FAMILY

from ..engine import dsl
from ..engine.cardrules import characteristics
from ..engine.coverage import Status
from ..judge import CompiledCard, DeckEntry

DIRECTORY = Path(__file__).resolve().parent

@dataclass(frozen=True)
class Benchmark:
    label: str
    # The format family it sits in: 100-card Commander decks or 60-card decks.
    family: str


BENCHMARKS = {
    "aggro": Benchmark("Benchmark: Aggro", COMMANDER_FAMILY),
    "midrange": Benchmark("Benchmark: Midrange", COMMANDER_FAMILY),
    "control": Benchmark("Benchmark: Control", COMMANDER_FAMILY),
    "aggro60": Benchmark("Benchmark: Aggro (60)", CONSTRUCTED_FAMILY),
    "midrange60": Benchmark("Benchmark: Midrange (60)", CONSTRUCTED_FAMILY),
    "control60": Benchmark("Benchmark: Control (60)", CONSTRUCTED_FAMILY),
}

# The raw Scryfall fields the pre-pass reads; the rest of the card is not kept.
RAW_FIELDS = (
    "name",
    "oracle_id",
    "layout",
    "type_line",
    "mana_cost",
    "cmc",
    "oracle_text",
    "power",
    "toughness",
    "colors",
    "keywords",
    "produced_mana",
)
FACE_FIELDS = ("name", "type_line", "mana_cost", "oracle_text", "power", "toughness", "colors")


def choices(family: str | None = None) -> list[tuple[str, str]]:
    """``(key, label)`` of every benchmark, or of one format family's."""
    return [(key, b.label) for key, b in BENCHMARKS.items() if family is None or b.family == family]


def label(key: str) -> str:
    return BENCHMARKS[key].label if key in BENCHMARKS else key


def family(key: str) -> str:
    return BENCHMARKS[key].family


def slim_raw(card: dict) -> dict:
    """The part of a raw Scryfall card the benchmark files keep."""
    data = {k: card[k] for k in RAW_FIELDS if k in card}
    if card.get("card_faces"):
        data["card_faces"] = [{k: f[k] for k in FACE_FIELDS if k in f} for f in card["card_faces"]]
    return data


def rules_path(key: str) -> Path:
    return DIRECTORY / f"{key}.rules.json"


def decklist_path(key: str) -> Path:
    return DIRECTORY / f"{key}.txt"


@cache
def _load(key: str) -> dict:
    if key not in BENCHMARKS:
        raise ValueError(f"Unknown benchmark deck {key!r}.")
    return json.loads(rules_path(key).read_text(encoding="utf-8"))


def entries(key: str) -> list[DeckEntry]:
    """The compiled cards of a benchmark deck, read from its bundled file."""
    data = _load(key)
    if data.get("dsl_version") != dsl.DSL_VERSION:
        raise ValueError(f"The {key} benchmark was built for DSL v{data.get('dsl_version')}: rebuild it.")
    out = []
    for item in data["cards"]:
        facts = characteristics(item["card"])
        program = item.get("program")
        if program is not None:
            dsl.validate_program(program, facts)
        compiled = CompiledCard(
            name=facts.name,
            status=item["status"] if program is not None else Status.UNSUPPORTED,
            facts=facts,
            program=program,
            reason=item.get("reason", ""),
            reason_kind=item.get("reason_kind", ""),
            model=item.get("model", ""),
        )
        out.append(DeckEntry(item["quantity"], item["is_commander"], compiled, item["card"]))
    return out
