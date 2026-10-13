# Agents

An agent is whatever plays a seat. It never decides what is legal: Forge lists the
options of each decision (see [Forge engine](engine.md)), and the agent picks one of
them by index, or delegates the decision to Forge's AI.

**Every seat of a new match is Jev** (`playtest.forms.SEAT_AGENT`): the new-match form
offers decks, not agents or models, and ignores any posted agent or model: every seat
uses the server's `JEV_MODEL`. The other agents stay in the code for tests and evaluation.

| `Seat.Agent` | Class | Network | Notes |
|---|---|---|---|
| `jev` | `JevAgent` | OpenRouter | Every new seat. One Jev `choice` per decision; logs its confidence. |
| `llm` | `LLMAgent` | OpenRouter | Not selectable; kept for the evaluation in #44. One chat completion per decision; logs its reason. |
| `random` | `RandomAgent` | none | Not selectable; the seeded baseline of the runner and replay tests. |
| `forge_ai` | — | none | Older matches only. Such a seat is created with `controller: "forge_ai"` and never reaches a worker. |

## The interface

`playtest/agents/base.py`:

```python
@dataclass(frozen=True)
class Decision:
    choice: int | None          # the picked option's index; None delegates to Forge's AI
    reasoning: str = ""         # why, in the agent's own words (LLM)
    fallback_reason: str = ""   # set when it delegates: why, in words
    fallback_kind: str = ""     # "low_confidence", "error" or "budget"
    confidence: float | None = None                    # Jev
    probabilities: tuple[tuple[str, float], ...] = ()  # top options by label
    model: str = ""             # the versioned model id that answered

    delegate: bool              # property: choice is None
    def meta(self) -> dict: ... # what the bridge stores on the event

def delegate(kind, reason, **extra) -> Decision: ...

class Agent(Protocol):
    def choose(self, view: dict, options: list[dict], *, kind: str = "", prompt: str = "") -> Decision: ...
```

- `view`, `options` (`index`, `label`, `facts`), `kind` and `prompt` are the bridge's
  decision as is. The bridge never asks when there is a single option.
- The worker answers `{"choice": i, "meta": ...}` or `{"delegate": true, "meta": ...}`;
  `meta` carries `confidence`, `probabilities`, `model`, `reason`,
  `fallback_reason` and `fallback_kind`, and lands on the bridge's `decision` or
  `fallback` event, which the match page and the summary read.

## Jev agent

`playtest/agents/jev_agent.py` — `JevAgent(model=None, *, seed=None, ask=None, min_confidence=None)`.

One Jev request per decision:

- **state** (`jev_state`): the view filtered to what a decision needs — turn, phase,
  active player; your life, hand with card text, library count, mana available,
  lands played, commanders with their tax; every battlefield with P/T, tapped,
  summoning sick, damage, counters and what an aura is attached to; opponents' hand
  and library counts; the stack and combat. Graveyards and exile are counts unless
  the decision is about them (a `choose_cards` or `target` prompt naming a graveyard
  or exile). No ids, no history.
- **one `choice` named `action`**: its instructions name the seat, the format, the
  turn, whose turn and phase it is and the goal, plus a line per decision kind
  (`KIND_LINES`) and Forge's prompt. Its options are exactly the bridge's, keyed
  `o<index>`, each described by its label **plus Forge's facts** (`describe_option`);
  the model computes nothing.
- the options are **shuffled with the agent's seeded RNG** before each request and
  mapped back by key, because Jev leans toward the first option and Forge's first is
  always "Pass" or "Done".

`gate()` is the one place an uncertain decision is handed over: below
`JEV_MIN_CONFIDENCE` (default 0.2) the agent delegates with `low_confidence`, keeping
Jev's confidence and top options in the fallback's meta. Jev's confidence measures
how peaked its distribution is, not the chosen option's probability. Measured on
Forge decisions (a Modern Burn seat against Forge's AI, 23 decisions, jev-1.13):
median 0.38, quartiles 0.20 / 0.82; 0.2 hands over 17% of decisions (near-ties
between two burn spells), 0.4 would hand over half — the same shape as on the old
Python kernel (222 decisions, median ~0.4). A missing key, a transport error, a malformed answer
or one naming no offered option delegates with `error`.

**Metering** happens in the worker (`playtest.workers.decide`) for agents marked
`metered` (Jev and LLM): past `MAX_AGENT_CALLS` (400) for the seat or
`MAX_MATCH_SECONDS` (1800) for the match, the seat delegates without calling the
model (`budget`).

## Jev client

`playtest/agents/typesafe.py` — `system_one(state, questions, *, model=None, key=None, sleep=time.sleep)`
against OpenRouter's `POST /api/v1/systemone`, which serves Jev in TypeSafe's native
`state` / `questions` format. No SDK.

| | |
|---|---|
| API key | `OPENROUTER_API_KEY` (shared with the LLM client) |
| Model | `JEV_MODEL`, else `~typesafe/jev-latest`; the response's versioned id is returned |
| Retries | up to 4 attempts on `429` / `529`, honouring `retry-after`, exponential back-off |
| Timeout | `REQUEST_TIMEOUT = 30` seconds per request |
| Limits | `choice()` refuses more than 255 options |

`read_choice(answer, options)` checks an answer names an offered option. Every
failure is a `TypeSafeError`.

## LLM agent

`playtest/agents/llm_agent.py` — `LLMAgent(model=None, *, seed=None, chat=None)`.
Each decision is one chat completion: a system message with a short brief and the
reply format `{"choice": <number>, "reason": "<one short sentence>"}`; a user message
with the format, the same state Jev sees, the decision kind, Forge's prompt and the
numbered options with their facts. `parse_choice` reads the first `{...}` block. On a
`ChatError` or an invalid reply it delegates (`error`). Whether it stays is decided
in #44.

## OpenRouter client

`playtest/agents/openrouter.py` — `chat(messages, model=None, key=None) -> str`
against `/chat/completions`. Default model `OPENROUTER_MODEL`, else
`google/gemini-2.5-flash`. Failures raise `ChatError`.

Without `OPENROUTER_API_KEY` the new-match form refuses Jev and LLM seats.

## Adding an agent

1. Write a class with `choose(view, options, *, kind="", prompt="") -> Decision`
   under `playtest/agents/`. Return an offered option's index, or `delegate(...)`
   with a reason. Set `metered = True` if it calls a model.
2. Add a value to `playtest.models.Seat.Agent` (a migration) and an icon in
   `Seat.agent_icon`.
3. Build it in `playtest.workers.build_agent` from the `Seat` and the match seed.
4. To let new matches use it, change `SEAT_AGENT` in `playtest/forms.py` (today every
   seat is Jev and the form offers no choice).
5. Test it without network: pass a fake `ask` / `chat`, or monkeypatch
   `typesafe.system_one` / `requests.post`, and play it against the `fake_forge`
   fixture (`tests/test_jev_agent.py`, `tests/test_runner.py`).
