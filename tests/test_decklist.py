"""Tests for decklist parsing."""

from decks.rules.decklist import parse_decklist_text


def _card(qty, name, is_commander=False):
    return {"quantity": qty, "name": name, "is_commander": is_commander}


def test_parses_quantity_and_name():
    text = "4 Lightning Bolt\n2 Counterspell\n"
    cards = parse_decklist_text(text)
    assert cards == [_card(4, "Lightning Bolt"), _card(2, "Counterspell")]


def test_parses_the_x_quantity_spelling():
    text = "1x Sol Ring\n12x Forest\n"
    assert parse_decklist_text(text) == [_card(1, "Sol Ring"), _card(12, "Forest")]


def test_line_without_quantity_defaults_to_one():
    text = "Sol Ring\n"
    assert parse_decklist_text(text) == [_card(1, "Sol Ring")]


def test_skips_blank_lines_and_comments():
    text = "\n// a comment\n# another\n1 Island\n"
    assert parse_decklist_text(text) == [_card(1, "Island")]


def test_commander_section_flags_its_cards():
    text = "Commander\n1 Atraxa, Praetors' Voice\n\nDeck\n1 Sol Ring\n"
    assert parse_decklist_text(text) == [
        _card(1, "Atraxa, Praetors' Voice", is_commander=True),
        _card(1, "Sol Ring"),
    ]


def test_cmdr_marker_flags_a_single_line():
    text = "1 Atraxa, Praetors' Voice *CMDR*\n1 Sol Ring\n"
    assert parse_decklist_text(text) == [
        _card(1, "Atraxa, Praetors' Voice", is_commander=True),
        _card(1, "Sol Ring"),
    ]


def test_sideboard_cards_still_count_towards_the_deck():
    # Commander has no sideboard: the header is skipped but its cards are kept,
    # so the 100-card rule catches the overflow.
    text = "Deck\n1 Forest\nSideboard\n2 Naturalize\n"
    assert parse_decklist_text(text) == [_card(1, "Forest"), _card(2, "Naturalize")]


def test_section_header_match_is_case_insensitive():
    text = "MAINBOARD\n1 Plains\ncommander:\n1 Ramos, Dragon Engine\n"
    assert parse_decklist_text(text) == [
        _card(1, "Plains"),
        _card(1, "Ramos, Dragon Engine", is_commander=True),
    ]


def test_strips_surrounding_whitespace():
    text = "   3    Birds of Paradise   \n"
    assert parse_decklist_text(text) == [_card(3, "Birds of Paradise")]


def test_card_name_with_digits():
    text = "1 Borrowing 100,000 Arrows\n"
    assert parse_decklist_text(text) == [_card(1, "Borrowing 100,000 Arrows")]


def test_empty_text_returns_empty_list():
    assert parse_decklist_text("") == []


def test_parse_decklist_text_parses_lines():
    cards = parse_decklist_text("4 Lightning Bolt\n// comment\n\n2 Island\n")
    assert cards == [_card(4, "Lightning Bolt"), _card(2, "Island")]


def test_parse_decklist_text_empty_returns_empty():
    assert parse_decklist_text("\n  \n# only comment\n") == []


def test_constructed_sideboard_is_kept_apart():
    text = "4 Lightning Bolt\n\nSideboard\n2 Pyroblast\n"
    assert parse_decklist_text(text, "modern") == [
        {"quantity": 4, "name": "Lightning Bolt", "is_commander": False, "board": "main"},
        {"quantity": 2, "name": "Pyroblast", "is_commander": False, "board": "side"},
    ]


def test_constructed_deck_header_switches_back_to_main():
    text = "Sideboard\n1 Duress\nDeck\n4 Thoughtseize\n"
    boards = [(c["name"], c["board"]) for c in parse_decklist_text(text, "legacy")]
    assert boards == [("Duress", "side"), ("Thoughtseize", "main")]


def test_constructed_maybeboard_is_ignored():
    text = "4 Ponder\nMaybeboard\n1 Brainstorm\n"
    assert [c["name"] for c in parse_decklist_text(text, "pauper")] == ["Ponder"]


def test_commander_formats_still_count_the_sideboard_as_deck():
    text = "Commander\n1 Atraxa, Praetors' Voice\nSideboard\n1 Sol Ring\n"
    cards = parse_decklist_text(text, "commander")
    assert [c.get("board") for c in cards] == [None, None]
    assert cards[1]["is_commander"] is False


def test_mtgo_lists_put_the_sideboard_after_a_blank_line():
    text = "4 Lightning Bolt\n56 Mountain\n\n4 Pyroblast\n"
    boards = [(c["name"], c["board"]) for c in parse_decklist_text(text, "modern")]
    assert boards == [("Lightning Bolt", "main"), ("Mountain", "main"), ("Pyroblast", "side")]


def test_a_blank_line_inside_a_headed_list_changes_nothing():
    text = "Deck\n4 Lightning Bolt\n\n56 Mountain\nSideboard\n4 Pyroblast\n"
    boards = [c["board"] for c in parse_decklist_text(text, "modern")]
    assert boards == ["main", "main", "side"]


def test_arena_companion_section_is_not_main_deck():
    text = "Companion\n1 Lurrus of the Dream-Den\n\nDeck\n4 Lightning Bolt\n\nSideboard\n1 Lurrus of the Dream-Den\n"
    cards = parse_decklist_text(text, "legacy")
    assert [(c["name"], c["board"]) for c in cards] == [("Lightning Bolt", "main"), ("Lurrus of the Dream-Den", "side")]
