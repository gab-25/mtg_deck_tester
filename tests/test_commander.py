"""Tests for the Commander format rules: color identity and deck legality."""

from decks.rules.commander import (
    card_color_identity,
    check_deck,
    check_decklist,
    commander_names,
    deck_color_identity,
)

ATRAXA = "Atraxa, Praetors' Voice"


def _entry(qty, name, is_commander=False):
    """One parsed decklist line."""
    return {"quantity": qty, "name": name, "is_commander": is_commander}


def _card(
    name,
    type_line="Creature — Human",
    identity=(),
    rules_text="",
    mana_cost="",
    card_id=None,
    legalities=None,
):
    card = {
        "id": card_id or name,
        "name": name,
        "type_line": type_line,
        "color_identity": list(identity),
        "faces": [
            {
                "name": name,
                "type_line": type_line,
                "rules_text": rules_text,
                "mana_cost": mana_cost,
            }
        ],
    }
    # Omitted entirely when not given: cards stored before legalities were
    # persisted have no such key.
    if legalities is not None:
        card["legalities"] = legalities
    return card


def _item(qty, card, is_commander=False):
    """One processed (fetched) card."""
    return {"quantity": qty, "is_commander": is_commander, "data": card}


def _legal_entries():
    """A 100-card singleton decklist with one commander."""
    entries = [_entry(1, ATRAXA, is_commander=True)]
    entries += [_entry(1, f"Spell {i}") for i in range(60)]
    entries.append(_entry(39, "Forest"))
    return entries


class TestCardColorIdentity:
    def test_uses_scryfall_identity_in_wubrg_order(self):
        card = _card("Atraxa", identity=("G", "W", "B", "U"))
        assert card_color_identity(card) == ["W", "U", "B", "G"]

    def test_colorless_card_has_empty_identity(self):
        assert card_color_identity(_card("Sol Ring", identity=())) == []

    def test_falls_back_to_mana_costs_when_identity_is_missing(self):
        # Decks stored before color_identity was persisted keep working.
        legacy = {"name": "Old Card", "faces": [{"mana_cost": "{1}{U}{R}"}]}
        assert card_color_identity(legacy) == ["U", "R"]


class TestDeckColorIdentity:
    def test_comes_from_the_commander_not_the_whole_deck(self):
        cards = [
            _item(1, _card(ATRAXA, identity=("W", "U")), is_commander=True),
            # An off-identity card must not widen the deck's identity.
            _item(1, _card("Lightning Bolt", identity=("R",))),
        ]
        assert deck_color_identity(cards) == ["W", "U"]

    def test_falls_back_to_the_deck_when_no_commander_is_declared(self):
        cards = [_item(1, _card("Lightning Bolt", identity=("R",)))]
        assert deck_color_identity(cards) == ["R"]

    def test_commander_names_lists_declared_commanders_only(self):
        cards = [
            _item(1, _card(ATRAXA), is_commander=True),
            _item(1, _card("Sol Ring")),
        ]
        assert commander_names(cards) == [ATRAXA]


class TestCheckDecklist:
    def test_a_legal_decklist_has_no_problems(self):
        assert check_decklist(_legal_entries()) == []

    def test_reports_a_deck_that_is_not_100_cards(self):
        entries = _legal_entries()
        entries[-1] = _entry(38, "Forest")  # 99 cards
        issues = check_decklist(entries)
        assert len(issues) == 1
        assert "99 cards" in issues[0]

    def test_reports_a_missing_commander(self):
        entries = [_entry(1, ATRAXA)] + _legal_entries()[1:]
        assert any("No commander declared" in issue for issue in check_decklist(entries))

    def test_reports_more_than_two_commanders(self):
        # A partner pair is two commanders; three is never legal.
        entries = _legal_entries()
        entries[1] = _entry(1, "Partner", is_commander=True)
        entries[2] = _entry(1, "Another", is_commander=True)
        issues = check_decklist(entries)
        assert len(issues) == 1
        assert "3 commanders declared" in issues[0]

    def test_reports_duplicates(self):
        entries = _legal_entries()
        entries[1] = _entry(2, "Sol Ring")
        entries[-1] = _entry(38, "Forest")  # keep the deck at 100 cards
        issues = check_decklist(entries)
        assert len(issues) == 1
        assert "Sol Ring" in issues[0] and "singleton" in issues[0]

    def test_basic_lands_and_any_number_cards_may_repeat(self):
        entries = [_entry(1, ATRAXA, is_commander=True)]
        entries += [_entry(1, f"Spell {i}") for i in range(30)]
        entries.append(_entry(39, "Snow-Covered Swamp"))
        entries.append(_entry(30, "Relentless Rats"))
        assert check_decklist(entries) == []

    def test_reports_every_problem_at_once(self):
        # 4 cards, no commander, two duplicate entries.
        entries = [_entry(2, "Sol Ring"), _entry(2, "Lightning Bolt")]
        assert len(check_decklist(entries)) == 4


class TestCheckDeck:
    def _legal_cards(self):
        cards = [
            _item(
                1,
                _card(ATRAXA, type_line="Legendary Creature — Phyrexian Angel Horror",
                      identity=("W", "U", "B", "G")),
                is_commander=True,
            )
        ]
        cards += [
            _item(1, _card(f"Spell {i}", identity=("G",))) for i in range(60)
        ]
        cards.append(_item(39, _card("Forest", type_line="Basic Land — Forest")))
        return cards

    def test_a_legal_deck_has_no_problems(self):
        assert check_deck(self._legal_cards()) == []

    def test_rejects_a_commander_that_is_not_legendary(self):
        cards = self._legal_cards()
        cards[0] = _item(
            1, _card("Llanowar Elves", type_line="Creature — Elf Druid"), is_commander=True
        )
        issues = check_deck(cards)
        assert any("cannot be a commander" in issue for issue in issues)

    def test_accepts_a_card_that_says_it_can_be_your_commander(self):
        cards = self._legal_cards()
        cards[0] = _item(
            1,
            _card(
                "Rowan, Scion of War",
                type_line="Legendary Planeswalker — Rowan",
                identity=("W", "U", "B", "G"),
                rules_text="Rowan, Scion of War can be your commander.",
            ),
            is_commander=True,
        )
        assert check_deck(cards) == []

    def test_reports_a_card_outside_the_commanders_color_identity(self):
        cards = self._legal_cards()
        cards[1] = _item(1, _card("Lightning Bolt", identity=("R",)))
        issues = check_deck(cards)
        assert len(issues) == 1
        assert "Lightning Bolt" in issues[0] and "color identity" in issues[0]

    def test_singleton_exemption_comes_from_the_rules_text(self):
        cards = self._legal_cards()
        cards[1] = _item(
            30,
            _card(
                "Relentless Rats",
                identity=("B",),
                rules_text="A deck can have any number of cards named Relentless Rats.",
            ),
        )
        # The deck is now oversized, but the duplicates are not a problem.
        issues = check_deck(cards)
        assert all("singleton" not in issue for issue in issues)


class TestBanList:
    """The Commander ban list, read from Scryfall's ``legalities.commander``."""

    def _legal_cards(self):
        return TestCheckDeck()._legal_cards()

    def test_reports_a_banned_card(self):
        cards = self._legal_cards()
        cards[1] = _item(
            1,
            _card("Black Lotus", type_line="Artifact",
                  legalities={"commander": "banned"}),
        )
        issues = check_deck(cards)
        assert len(issues) == 1
        assert "Black Lotus" in issues[0] and "banned in Commander" in issues[0]

    def test_reports_a_card_that_was_never_legal(self):
        cards = self._legal_cards()
        cards[1] = _item(
            1,
            _card("Nalathni Dragon", identity=("R",),
                  legalities={"commander": "not_legal"}),
        )
        issues = check_deck(cards)
        # The card is also off-identity; the legality problem is reported too.
        assert any(
            "Nalathni Dragon" in issue and "not legal in Commander" in issue
            for issue in issues
        )

    def test_reports_every_offending_card_at_once(self):
        cards = self._legal_cards()
        cards[1] = _item(1, _card("Mana Crypt", type_line="Artifact",
                                  legalities={"commander": "banned"}))
        cards[2] = _item(1, _card("Dockside Extortionist", identity=("R",),
                                  legalities={"commander": "banned"}))
        cards[3] = _item(1, _card("Sword of Dungeons & Dragons", type_line="Artifact",
                                  legalities={"commander": "not_legal"}))
        legality_issues = [
            issue for issue in check_deck(cards) if "Commander." in issue
        ]
        assert len(legality_issues) == 3

    def test_the_commander_itself_is_checked(self):
        cards = self._legal_cards()
        cards[0] = _item(
            1,
            _card(ATRAXA, type_line="Legendary Creature — Phyrexian Angel Horror",
                  identity=("W", "U", "B", "G"),
                  legalities={"commander": "banned"}),
            is_commander=True,
        )
        issues = check_deck(cards)
        assert len(issues) == 1
        assert ATRAXA in issues[0] and "banned in Commander" in issues[0]

    def test_a_card_without_legalities_passes(self):
        # Decks analyzed before the field was persisted must not start failing.
        assert check_deck(self._legal_cards()) == []

    def test_an_unknown_legality_value_passes(self):
        cards = self._legal_cards()
        cards[1] = _item(1, _card("Sol Ring", legalities={"commander": "restricted"}))
        assert check_deck(cards) == []

    def test_an_empty_legalities_dict_passes(self):
        cards = self._legal_cards()
        cards[1] = _item(1, _card("Sol Ring", legalities={}))
        assert check_deck(cards) == []

    def test_duel_commander_reads_its_own_ban_list(self):
        # Sol Ring is the case that proves the feature: legal in Commander,
        # banned in Duel Commander, same card, same deck.
        sol_ring = _card(
            "Sol Ring",
            type_line="Artifact",
            legalities={"commander": "legal", "duel": "banned"},
        )
        cards = self._legal_cards()
        cards[1] = _item(1, sol_ring)

        assert check_deck(cards) == []
        assert check_deck(cards, fmt="commander") == []

        issues = check_deck(cards, fmt="duel")
        assert len(issues) == 1
        assert "Sol Ring" in issues[0]

    def test_the_message_names_the_format(self):
        cards = self._legal_cards()
        cards[1] = _item(
            1, _card("Sol Ring", type_line="Artifact", legalities={"duel": "banned"})
        )
        assert "banned in Duel Commander." in check_deck(cards, fmt="duel")[0]

    def test_a_card_without_legalities_passes_in_every_format(self):
        for key in ("commander", "duel"):
            assert check_deck(self._legal_cards(), fmt=key) == []

    def test_a_legal_card_passes(self):
        cards = self._legal_cards()
        cards[1] = _item(1, _card("Sol Ring", legalities={"commander": "legal"}))
        assert check_deck(cards) == []


class TestDoubleFacedCards:
    """Both faces of a card are one card: one line, one copy, one front face."""

    def _legal_cards(self):
        return TestCheckDeck()._legal_cards()

    def test_a_card_listed_twice_breaks_the_singleton_rule(self):
        cards = self._legal_cards()
        # Two lines of one copy each: still two copies of the same card.
        cards[1] = _item(1, _card("Sol Ring", identity=()))
        cards[2] = _item(1, _card("Sol Ring", identity=()))
        issues = check_deck(cards)
        assert len(issues) == 1
        assert "2×" in issues[0] and "Sol Ring" in issues[0]

    def test_a_double_faced_card_listed_under_both_spellings_is_one_card(self):
        cards = self._legal_cards()
        full = "Sink into Stupor // Soporific Springs"
        cards[1] = _item(1, _card("Sink into Stupor", type_line="Instant",
                                  identity=("U",), card_id="mdfc-1"))
        cards[2] = _item(1, _card(full, type_line="Instant // Land",
                                  identity=("U",), card_id="mdfc-1"))
        issues = check_deck(cards)
        assert len(issues) == 1
        assert "singleton" in issues[0]

    def test_a_commander_must_be_legendary_on_its_front_face(self):
        # Bloodline Keeper transforms into a legendary creature, which does not
        # make the card a legal commander.
        cards = self._legal_cards()
        cards[0] = _item(
            1,
            _card(
                "Bloodline Keeper // Lord of Lineage",
                type_line="Creature — Vampire // Legendary Creature — Vampire",
                identity=("W", "U", "B", "G"),
            ),
            is_commander=True,
        )
        issues = check_deck(cards)
        assert any("cannot be a commander" in issue for issue in issues)

    def test_the_decklist_pass_tallies_copies_across_lines(self):
        entries = _legal_entries()
        entries[1] = _entry(1, "Sink into Stupor")
        entries[2] = _entry(1, "Sink into Stupor // Soporific Springs")
        issues = check_decklist(entries)
        assert len(issues) == 1
        assert "singleton" in issues[0]

    def test_two_lines_of_the_same_name_are_reported_once(self):
        entries = _legal_entries()
        entries[1] = _entry(1, "Sol Ring")
        entries[2] = _entry(1, "Sol Ring")
        issues = check_decklist(entries)
        assert len(issues) == 1
        assert "2×" in issues[0]



# -- partner commanders --------------------------------------------------------


def _deck_with(*commanders):
    items = [_item(1, c, is_commander=True) for c in commanders]
    items.append(_item(100 - len(commanders), _card("Forest", "Basic Land — Forest")))
    return items


def _legend(name, text=""):
    return _card(name, "Legendary Creature — Human", rules_text=text)


def test_two_partners_are_a_legal_pair():
    assert check_deck(_deck_with(_legend("Kraum", "Partner"), _legend("Tymna", "Partner"))) == []


def test_partner_with_must_name_the_other():
    ok = _deck_with(_legend("Pir", "Partner with Toothy"), _legend("Toothy", "Partner with Pir"))
    assert check_deck(ok) == []
    bad = _deck_with(_legend("Pir", "Partner with Toothy"), _legend("Kraum", "Partner"))
    assert any("cannot share" in i for i in check_deck(bad))


def test_a_background_pairs_with_a_commander_that_chooses_one():
    background = _card("Cloakwood", "Legendary Enchantment — Background")
    chooser = _legend("Wilson", "Choose a Background")
    assert check_deck(_deck_with(chooser, background)) == []


def test_two_commanders_without_partner_are_refused():
    issues = check_deck(_deck_with(_legend("A"), _legend("B")))
    assert any("cannot share" in i for i in issues)


def test_the_decklist_pass_accepts_two_commanders():
    entries = [_entry(1, "A", True), _entry(1, "B", True), _entry(98, "Forest")]
    assert check_decklist(entries) == []


def test_partner_identity_is_the_union():
    a = _card("Kraum", "Legendary Creature — Zombie", identity=("U", "R"), rules_text="Partner")
    b = _card("Tymna", "Legendary Creature — Human", identity=("W", "B"), rules_text="Partner")
    assert deck_color_identity([_item(1, a, True), _item(1, b, True)]) == ["W", "U", "B", "R"]
