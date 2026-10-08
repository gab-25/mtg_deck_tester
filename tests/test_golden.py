"""Golden games: whole games under the heuristic agent, no network, fully deterministic."""

import json

import pytest

from playtest.agents.heuristic import HeuristicAgent
from playtest.engine.events import EventKind
from playtest.engine.game import EndReason, Game
from playtest.engine.state import GameRules

from .factories import FOREST, creature, engine_deck

COMMANDER = GameRules(starting_life=40, commander_damage_limit=21)
DUEL = GameRules(starting_life=20, commander_damage_limit=None)
# Nothing in a clean game should trip a cap or need a warning.
UNEXPECTED = {EventKind.WARNING, EventKind.LIMIT, EventKind.DECISION_LIMIT, EventKind.AGENT_FALLBACK}


def _play(players, seed, **kwargs):
    decks = [engine_deck(name) for name in "ABCD"[:players]]
    rules = DUEL if players == 2 else COMMANDER
    agents = [HeuristicAgent() for _ in decks]
    return Game(decks, agents, rules, seed=seed, check_invariants=True, **kwargs).run()


def _log(result):
    return [(e.seq, e.round, e.turn, e.step, e.event.kind, e.event.seat, e.event.text, e.event.payload) for e in result.events]


def test_the_same_seed_replays_event_for_event():
    assert _log(_play(4, 11, max_rounds=40)) == _log(_play(4, 11, max_rounds=40))


@pytest.mark.parametrize("players", [2, 4])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_heuristic_agents_play_to_a_winner(players, seed):
    result = _play(players, seed, max_rounds=60)
    assert result.end_reason == EndReason.LAST_STANDING
    assert result.winner_seat is not None
    kinds = {e.event.kind for e in result.events}
    assert not kinds & UNEXPECTED
    # Payloads are stored as they are in MatchEvent.payload.
    for event in result.events:
        assert json.loads(json.dumps(event.event.payload)) == event.event.payload
    losers = [s for s in result.seats if s.seat != result.winner_seat]
    assert all(s.eliminated_round is not None for s in losers)


ELVES = engine_deck(
    "Elves",
    library=[FOREST] * 5
    + [
        creature("Grizzly Bears", 2),
        creature("Trained Armodon", 3, mana_value=3),
        creature("Giant Spider", 2, 4, mana_value=4, keywords=["reach"]),
        creature("Elvish Warrior", 2, 3),
        creature("Llanowar Elves", 1, mana_value=1),
    ],
    commander=creature("Elf Lord", 3, mana_value=3, legendary=True),
)
BEASTS = engine_deck(
    "Beasts",
    library=[FOREST] * 5
    + [
        creature("Wild Boar", 2),
        creature("Kraul Swarm", 2, 1, mana_value=3, keywords=["flying"]),
        creature("Craw Wurm", 6, 4, mana_value=6),
        creature("Scaled Wurm", 7, 6, mana_value=7),
        creature("Gnarlid", 2, mana_value=2, keywords=["menace"]),
    ],
    commander=creature("Beast Lord", 4, mana_value=4, legendary=True, keywords=["trample"]),
)

# Read and checked by hand: each line follows from the rules and the agent's.
EXPECTED_DUEL = [
    ('', '2 players at 20 life. Beasts goes first.'),
    ('', "Round 1: Beasts's turn."),
    ('main1', 'Beasts plays Forest.'),
    ('', "Round 1: Elves's turn."),
    ('draw', 'Elves draws a card.'),
    ('main1', 'Elves plays Forest.'),
    ('', "Round 2: Beasts's turn."),
    ('draw', 'Beasts draws a card.'),
    ('main1', 'Beasts plays Forest.'),
    ('main1', 'Beasts casts Gnarlid, tapping Forest, Forest.'),
    ('main1', "Gnarlid resolves and enters the battlefield under Beasts's control."),
    ('', "Round 2: Elves's turn."),
    ('draw', 'Elves draws a card.'),
    ('main1', 'Elves plays Forest.'),
    ('main1', 'Elves casts Elvish Warrior, tapping Forest, Forest.'),
    ('main1', "Elvish Warrior resolves and enters the battlefield under Elves's control."),
    ('', "Round 3: Beasts's turn."),
    ('draw', 'Beasts draws a card.'),
    ('main1', 'Beasts plays Forest.'),
    ('main1', 'Beasts casts Kraul Swarm, tapping Forest, Forest, Forest.'),
    ('main1', "Kraul Swarm resolves and enters the battlefield under Beasts's control."),
    ('declare_attackers', 'Beasts attacks Elves with Gnarlid (2/2).'),
    ('declare_blockers', 'Elves does not block.'),
    ('combat_damage', 'Gnarlid deals 2 damage to Elves (18 life left).'),
    ('', "Round 3: Elves's turn."),
    ('draw', 'Elves draws a card.'),
    ('main1', 'Elves plays Forest.'),
    ('main1', 'Elves casts Elf Lord from the command zone, tapping Forest, Forest, Forest.'),
    ('main1', "Elf Lord resolves and enters the battlefield under Elves's control."),
    ('declare_attackers', 'Elves attacks Beasts with Elvish Warrior (2/3).'),
    ('declare_blockers', 'Beasts does not block.'),
    ('combat_damage', 'Elvish Warrior deals 2 damage to Beasts (18 life left).'),
    ('', "Round 4: Beasts's turn."),
    ('draw', 'Beasts draws a card.'),
    ('main1', 'Beasts plays Forest.'),
    ('main1', 'Beasts casts Beast Lord from the command zone, tapping Forest, Forest, Forest, Forest.'),
    ('main1', "Beast Lord resolves and enters the battlefield under Beasts's control."),
    ('declare_attackers', 'Beasts attacks Elves with Gnarlid (2/2), Kraul Swarm (2/1).'),
    ('declare_blockers', 'Elves does not block.'),
    ('combat_damage', 'Gnarlid deals 2 damage to Elves (16 life left).'),
    ('combat_damage', 'Kraul Swarm deals 2 damage to Elves (14 life left).'),
    ('', "Round 4: Elves's turn."),
    ('draw', 'Elves has to draw from an empty library.'),
    ('draw', 'Elves is eliminated: drew from an empty library.'),
    ('draw', 'Beasts wins in round 4.'),
]


def test_a_scripted_ten_card_duel_matches_the_expected_game():
    game = Game([ELVES, BEASTS], [HeuristicAgent(), HeuristicAgent()], DUEL, seed=7, max_rounds=10, check_invariants=True)
    result = game.run()
    assert [(e.step, e.event.text) for e in result.events] == EXPECTED_DUEL
    assert result.winner_seat == 1
