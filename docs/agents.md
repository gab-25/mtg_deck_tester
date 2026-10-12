# Agents

An agent is whatever plays a seat. The engine never trusts it: it offers a list of
legal actions, the agent picks one, and anything not on the list fails the game.

## The interface

`playtest/agents/base.py`:

```python
@dataclass(frozen=True)
class Decision:
    action: Action
    reasoning: str = ""        # why, in the agent's own words
    fallback_reason: str = ""  # set when a fallback chose instead

class Agent(Protocol):
    def choose(self, view: dict, options: list[Action]) -> Decision: ...
```

- `view` is `playtest.engine.view.player_view(state, seat)`: plain JSON-ready data,
  no hidden information (see [Game engine](engine.md#what-a-seat-can-see)).
- `options` is `legal_actions(...)`, never empty; `pass` is always last. The engine
  does not call the agent when `pass` is the only option.
- The returned `action` must be one of `options` (compared by `kind`, `card_uid`,
  `target_seat`; the `label` is ignored).

It is a `Protocol`: an agent is any object with a matching `choose` method.

## Random agent

`playtest/agents/random_agent.py` — `RandomAgent(seed)` picks uniformly with its own
`random.Random(seed)`. It is the free baseline, and the fallback of the LLM agent.

The runner seeds each seat with `match.seed + seat.position`, so two random seats do
not mirror each other and the same match seed replays the same game, event for
event.

## LLM agent

`playtest/agents/llm_agent.py` — `LLMAgent(model=None, *, seed=None, chat=None)`.

Each decision is **one chat completion** with two messages (`build_messages`):

- **system**: a short brief ("play to win…"), the engine rules — the docstring of
  `playtest/engine/rules.py`, imported verbatim as `RULES_TEXT` — and the required
  reply format: `{"choice": <number>, "reason": "<one short sentence>"}`.
- **user**: the view as indented JSON, then the options numbered from 1 by their
  `label`.

`parse_choice` takes the first `{...}` block of the reply (models often wrap JSON in
prose or code fences), accepts `choice` as an int or a digit string, checks it is in
range, and returns the 0-based index plus the reason. The reason becomes the
`Decision.reasoning`, which the engine logs on the `decision` / `pass` event and the
match page shows.

There is no conversation memory: every decision is a fresh, stateless request.

### Fallback

The agent never blocks a game. If the chat call raises `openrouter.ChatError` (no
key, network error, non-200, empty or malformed response) or `parse_choice` raises
`ValueError` (no JSON, invalid choice), it:

1. logs a warning (`LLM agent (<model>) fell back to random: <reason>`);
2. asks its internal `RandomAgent(seed)` instead;
3. returns that action with `fallback_reason` set.

The engine then records an `agent_fallback` event with the reason, so a fallback is
visible in the match log. Any other exception is not caught and fails the match.

## OpenRouter client

`playtest/agents/openrouter.py` — one function, `chat(messages, model=None, key=None) -> str`,
against OpenRouter's OpenAI-compatible `/chat/completions` endpoint. The model is a
setting, not a dependency: any id from <https://openrouter.ai/models> works.

| | |
|---|---|
| API key | `OPENROUTER_API_KEY` (`api_key()` returns `None` when unset or empty) |
| Default model | `OPENROUTER_MODEL`, else `DEFAULT_MODEL = "google/gemini-2.5-flash"` (`default_model()`) |
| Per-seat model | `Seat.model`; empty means the default |
| Timeout | `REQUEST_TIMEOUT = 60` seconds per request |
| Attribution | `HTTP-Referer` / `X-Title` headers |

Every failure is raised as `ChatError` with a readable message (the HTTP body is
truncated to 300 characters).

Without a key, the new-match form refuses LLM seats ("LLM seats need an OpenRouter
API key") and the home page shows "LLM players: Off".

**Cost.** One request per decision, up to `max_decisions_per_turn` (30) per turn,
for every turn of every LLM seat until `max_rounds`. Keep round limits low while
experimenting.

## Adding an agent

1. Write a class with `choose(view, options) -> Decision` under `playtest/agents/`.
   Return one of `options`; put any explanation in `reasoning`; if it has to fall
   back, say why in `fallback_reason`.
2. Add a value to `playtest.models.Seat.Agent` and generate a migration (the field's
   `choices` are part of the migration state).
3. Build it in `playtest.runner.build_agent` from the `Seat` and the match seed.
4. If it needs configuration on the form (like the LLM's model), extend
   `playtest/forms.py` and `playtest/templates/playtest/match_new.html`.
5. Test it without network: `tests/test_agents.py` passes a fake `chat` callable to
   `LLMAgent`, the OpenRouter tests monkeypatch `requests.post`, and
   `tests/test_matches_web.py` monkeypatches `openrouter.chat` for a full LLM match.
