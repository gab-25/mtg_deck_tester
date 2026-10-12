"""Tests for the constructed formats' deck-construction rules."""

from decks.rules.legality import check_deck, check_decklist


def _entry(qty, name, board="main", is_commander=False):
    return {"quantity": qty, "name": name, "is_commander": is_commander, "board": board}


def _item(qty, name, board="main", legal="legal", fmt="modern", rules_text="", card_id=None):
    type_line = "Basic Land — Mountain" if name == "Mountain" else "Instant"
    data = {
        "id": card_id or name,
        "name": name,
        "type_line": type_line,
        "faces": [{"name": name, "type_line": type_line, "rules_text": rules_text, "mana_cost": "{R}"}],
        "legalities": {fmt: legal},
    }
    return {"quantity": qty, "is_commander": False, "board": board, "data": data}


def _legal_entries():
    return [_entry(4, f"Spell {i}") for i in range(9)] + [_entry(24, "Mountain"), _entry(4, "Spare", "side")]


def test_a_legal_modern_deck_passes():
    assert check_decklist(_legal_entries(), "modern") == []
    items = [_item(e["quantity"], e["name"], e["board"]) for e in _legal_entries()]
    assert check_deck(items, "modern") == []


def test_every_problem_is_reported_at_once():
    # 8x4 + 3 + 23 + the commander line = 59 main-deck cards.
    entries = [_entry(4, f"Spell {i}") for i in range(8)] + [_entry(3, "Bolt"), _entry(23, "Mountain")]
    entries += [_entry(2, "Bolt", "side")] + [_entry(1, f"Side {i}", "side") for i in range(14)]
    entries += [_entry(1, "Atraxa", is_commander=True)]
    issues = check_decklist(entries, "modern")
    assert any("59" in i and "60" in i for i in issues), issues
    assert any("16" in i and "15" in i for i in issues), issues
    assert any("Bolt" in i and "5" in i for i in issues), issues
    assert any("commander" in i.lower() for i in issues), issues


def test_main_and_sideboard_copies_add_up():
    entries = [_entry(4, f"Spell {i}") for i in range(8)] + [_entry(3, "Bolt"), _entry(25, "Mountain"), _entry(2, "Bolt", "side")]
    assert [i for i in check_decklist(entries, "modern") if "Bolt" in i]


def test_banned_and_not_legal_cards_are_named():
    items = [_item(4, f"Spell {i}") for i in range(9)] + [_item(24, "Mountain")]
    items += [_item(1, "Bad", legal="banned"), _item(1, "Silver", "side", legal="not_legal")]
    issues = check_deck(items, "modern")
    assert "“Bad” is banned in Modern." in issues
    assert "“Silver” is not legal in Modern." in issues


def test_vintage_restricted_cards_are_limited_to_one():
    items = [_item(4, f"Spell {i}", fmt="vintage") for i in range(9)] + [_item(23, "Mountain", fmt="vintage")]
    items += [_item(2, "Ancestral Recall", legal="restricted", fmt="vintage")]
    issues = check_deck(items, "vintage")
    assert any("Ancestral Recall" in i and "restricted" in i for i in issues), issues


def test_commander_formats_still_use_the_commander_rules():
    from decks.rules import commander

    entries = [{"quantity": 1, "name": "X", "is_commander": True}]
    assert check_decklist(entries, "commander") == commander.check_decklist(entries)
