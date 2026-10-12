"""A synthetic deck whose cards really do things (compiled by the judge, no network)."""

from playtest.engine.cards import DeckSpec, card

from .cards import compiled_spec

MOUNTAIN = card("Mountain", "Basic Land", produces="R")


def spell_deck(name="Spells"):
    spells = [
        compiled_spec("Bolt", "Instant", "{R}", "Bolt deals 3 damage to any target."),
        compiled_spec("Kill", "Instant", "{1}{R}", "Destroy target creature."),
        compiled_spec("Think", "Sorcery", "{2}{R}", "Draw two cards."),
        compiled_spec("No", "Instant", "{R}{R}", "Counter target spell."),
        compiled_spec("Call", "Sorcery", "{2}{R}", "Create two 1/1 red Goblin creature tokens."),
        compiled_spec("Blast", "Sorcery", "{X}{R}", "Blast deals X damage to any target."),
        compiled_spec("Rush", "Instant", "{R}", "Target creature gets +2/+0 and gains haste until end of turn."),
        compiled_spec("Anthem", "Enchantment", "{2}{R}", "Creatures you control get +1/+1."),
        compiled_spec("Eater", "Creature — Horror", "{3}{R}", "When this creature enters, destroy target creature an opponent controls.", "2", "2"),
        compiled_spec("Martyr", "Creature — Cleric", "{1}{R}", "When this creature dies, you gain 3 life.", "2", "1"),
        compiled_spec("Pinger", "Creature — Goblin", "{2}{R}", "{T}: This creature deals 1 damage to any target.", "1", "1"),
        compiled_spec("Choice", "Instant", "{1}{R}", "Choose one —\n• Destroy target artifact.\n• Choice deals 2 damage to any target."),
        card("Brute", "Creature", "{3}{R}", 4, 3, keywords=("menace",)),
        card("Flyer", "Creature", "{2}{R}", 2, 2, keywords=("flying",)),
        card("Striker", "Creature", "{1}{R}", 2, 1, keywords=("first strike",)),
        card("Giant", "Creature", "{4}{R}", 5, 5, keywords=("trample",)),
    ]
    assert all(s.program is not None for s in spells), [s.name for s in spells if s.program is None]
    library = [MOUNTAIN] * 37 + [spells[i % len(spells)] for i in range(62)]
    commander = card(f"{name} Commander", "Legendary Creature", "{3}{R}", 4, 4, keywords=("haste",))
    return DeckSpec(name=name, commander=commander, library=tuple(library))
