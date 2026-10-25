"""scripts/seed_activity_sources.py: the npc_list rows for non-monster sources.

A source missing here is rejected as an unknown NPC, one item at a time, with
nothing but a new_npc row to show for it (t382). These pin the list's shape:
ids stay unique and in the reserved range, and "Loot Chest" stays out, because
opened PvP loot keys belong to data/submissions/pvp.py.
"""

from scripts import seed_activity_sources as seed

ALL = seed.ACTIVITY_SOURCES + seed.RUNELITE_EVENT_SOURCES


def test_ids_and_names_are_unique():
    ids = [npc_id for _, npc_id in ALL]
    names = [name.lower() for name, _ in ALL]
    assert len(set(ids)) == len(ids)
    assert len(set(names)) == len(names)


def test_ids_sit_in_the_reserved_range_without_gaps():
    ids = sorted(npc_id for _, npc_id in ALL)
    assert ids[0] == 900001
    assert ids == list(range(900001, 900001 + len(ids)))


def test_released_ids_never_move():
    # Rows already in production: renumbering would orphan their drops.
    assert dict(seed.ACTIVITY_SOURCES)["Fractured Archive"] == 900006
    assert dict(seed.RUNELITE_EVENT_SOURCES)["Rusty chest"] == 900007
    assert dict(seed.RUNELITE_EVENT_SOURCES)["Dirty arrowtips"] == 900050


def test_opened_loot_keys_are_not_an_npc():
    assert "loot chest" not in {name.lower() for name, _ in ALL}
