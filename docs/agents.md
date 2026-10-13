# Agents

An agent is whatever plays a seat. The engine never trusts it: it offers a list of
legal actions, the agent picks one, and anything not on the list fails the game.

| `Seat.Agent` | Class | Network | Notes |
|---|---|---|---|
| `heuristic` (default) | `HeuristicAgent` | none | Fixed rules; the Jev agent's fallback; drives every test. |
| `jev` | `JevAgent` | OpenRouter | One Jev `choice` per decision; logs its confidence. |
| `llm` | `LLMAgent` | OpenRouter | One chat completion per decision; logs its reason. |
| `random` | `RandomAgent` | none | The free baseline. |

## The interface

`playtest/agents/base.py`:

```python
@dataclass(frozen=True)
class Decision:
    action: Action
    reasoning: str = ""         # why, in the agent's own words (LLM)
    fallback_reason: str = ""   # set when another agent chose instead
    fallback_agent: str = ""    # "heuristic", "random"
    fallback_kind: str = ""     # "error" or "low_confidence"
    confidence: float | None = None              # Jev
    probabilities: tuple[tuple[str, float], ...] = ()  # top actions by label
    model: str = ""             # the versioned model id that answered

class Agent(Protocol):
    def choose(self, view: dict, options: list[Action]) -> Decision: ...
```

- `view` is `player_view(state, seat)` (see [Game engine](engine.md#what-a-seat-can-see)).
- `options` is `legal_actions(...)`: never empty, at most 255, the do-nothing action
  first. The engine does not call the agent when there is a single option.
- The returned `action` must be one of `options` (the `label` is not compared).

The engine logs a `decision` event (or `pass`) for every decision with a reason or a
confidence, and an `agent_fallback` event whenever `fallback_reason` is set; both
carry `Decision.payload()` — confidence, top probabilities, model, fallback kind —
which the match page and the summary read.

## Heuristic agent

`playtest/agents/heuristic.py` — deterministic ordered rules, first match wins,
ties broken by name and id: land → commander → removal on the best target →
highest-MV creature → other useful spell → counter an opponent's spell → safe or
lethal attacks at the opponent it pays most to attack (even trades when it
outnumbers the blockers) → blocks to survive and favourable trades → cycle what it
will not cast soon → pass. It pays alternative costs only when it cannot pay the
mana cost, weighs a sacrificed permanent as its removal value, and answers an
effect's choice with the kernel's highest estimate.

Its evaluations are **pure public helpers over the view** — `combat_outcome`,
`attack_facts`, `lethal_on_board`, `block_facts`, `removal_value`,
`incoming_damage`, `action_value` — so the Jev agent reuses them to annotate each
option.

## Jev agent

`playtest/agents/jev_agent.py` — `JevAgent(model=None, *, seed=None, ask=None, min_confidence=None)`.

One Jev request per decision:

- **state**: the view filtered to what a decision needs (life, mana, hand with
  rendered effects, battlefields, stack); no ids, no history, no oracle text;
- **one `choice` named `action`**, whose instructions name the format, the active
  player and the step; its options are exactly the legal actions, keyed `a0…aN`,
  described by their label **plus facts computed in code** ("lethal if unblocked",
  "dies to: X", "kills Y", "you take 5 combat damage (life 7 -> 2)");
- the options are **shuffled with the agent's seeded RNG** before each request and
  mapped back by key, because Jev leans toward the first option;
- when an effect asks a choice while it resolves (a search, a discard, a scry, a
  "you may"), the state carries its prompt (`choosing`) and each option the
  kernel's estimate ("estimated value 7").

`gate()` is the one place an uncertain decision is handed over: below
`JEV_MIN_CONFIDENCE` (default 0.2) the heuristic agent decides, with a
`low_confidence` fallback that keeps Jev's distribution. Jev's confidence measures
how peaked its distribution is, not the chosen option's probability; on 222
measured decisions its median was ~0.4. A missing key, a transport error or a
malformed answer hands the decision to the heuristic agent too (`error`), never to
the random agent.

The agent is `metered`: the game hands it to its fallback past `max_agent_calls`
(400) for the seat or `max_wall_clock_seconds` (300) for the match.

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
failure is a `TypeSafeError`. The card judge uses the same client
([Card compilation](card-compilation.md)).

## LLM agent

`playtest/agents/llm_agent.py` — `LLMAgent(model=None, *, seed=None, chat=None)`.
Each decision is one chat completion: a system message with a short brief, the
engine rules (the docstring of `playtest/engine/rules.py`) and the reply format
`{"choice": <number>, "reason": "<one short sentence>"}`; a user message with the
format, the view as JSON and the numbered options. `parse_choice` reads the first
`{...}` block. On a `ChatError` or an invalid reply it falls back to its internal
`RandomAgent`. It is kept as it is; whether it moves onto the heuristic fallback or
is removed is decided in #44.

## OpenRouter client

`playtest/agents/openrouter.py` — `chat(messages, model=None, key=None) -> str`
against `/chat/completions`. Default model `OPENROUTER_MODEL`, else
`google/gemini-2.5-flash`; per-seat `Seat.model`. Failures raise `ChatError`.

Without `OPENROUTER_API_KEY` the new-match form refuses Jev and LLM seats, the
home page shows the model players as off, and the card judge leaves cards that need
it unsupported (retried once a key is set).

## Adding an agent

1. Write a class with `choose(view, options) -> Decision` under `playtest/agents/`.
   Return one of `options`; explain fallbacks in `fallback_reason`. Set
   `metered = True` and a `fallback` attribute if it calls a model.
2. Add a value to `playtest.models.Seat.Agent` (a migration) and an icon in
   `Seat.agent_icon`.
3. Build it in `playtest.runner.build_agent` from the `Seat` and the match seed.
4. Extend `playtest/forms.py` and `match_new.html` if it needs configuration.
5. Test it without network: pass a fake `ask` / `chat`, or monkeypatch
   `requests.post` (`tests/test_jev_agent.py`, `tests/test_agents.py`).
