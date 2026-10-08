"""The Magic rules kernel.

Pure Python: no Django, no I/O, and no randomness except the seeded
``random.Random`` instances a :class:`~.game.Game` derives from its seed, one
per purpose. Agents decide, the engine enforces. The rules it knows are listed
in :mod:`.rules`; card effects are compiled elsewhere (:mod:`.dsl`) and are not
played yet.
"""
