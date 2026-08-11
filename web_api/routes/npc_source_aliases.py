"""Display aliases for NPC loot *sources* that players know by another name.

Some activities record their loot under container/reward NPCs rather than the
activity itself — Wintertodt drops land on ``Reward cart (Wintertodt)`` and
``Supply crate (Wintertodt)``; there is no ``npc_list`` row literally named
"Wintertodt". Players search for (and expect to see) "Wintertodt", so:

- read surfaces (item source lists, NPC search) can present one merged alias
  entry, carrying its real ``members`` so the UI keeps storing real names;
- write validation accepts the alias name and expands it to the member names
  (``expand_source_names``), so stored configs and the event engine keep
  matching drops by the *recorded* source name — the alias never leaks into a
  task config.

Intentionally dependency-free (importable from ``event_task_validation``,
whose test conftest stubs the whole ``services`` package).
"""
from __future__ import annotations

#: Each group: the player-facing alias, a representative npc id (for the
#: icon), and the real ``npc_list`` names its drops are recorded under.
NPC_SOURCE_ALIASES: tuple[dict, ...] = (
    {
        "name": "Wintertodt",
        "npc_id": 13974,  # Reward cart (Wintertodt) — the icon representative
        "members": ("Reward cart (Wintertodt)", "Supply crate (Wintertodt)"),
    },
)

#: CDN base for NPC icons. Duplicated (not imported) to keep this module
#: dependency-free — `items.py`/`search.py` each define their own the same way.
_IMG_BASE = "https://www.droptracker.io/img"

_BY_ALIAS = {g["name"].lower(): g for g in NPC_SOURCE_ALIASES}
_BY_MEMBER = {m.lower(): g for g in NPC_SOURCE_ALIASES for m in g["members"]}


def alias_group_by_name(name) -> dict | None:
    """The alias group whose display name is ``name`` (case-insensitive)."""
    return _BY_ALIAS.get(str(name or "").strip().lower())


def alias_group_for_member(name) -> dict | None:
    """The alias group containing real NPC ``name``, if any."""
    return _BY_MEMBER.get(str(name or "").strip().lower())


def expand_source_names(name) -> list[str]:
    """Alias display name -> its member (real) NPC names; else ``[name]``.

    Write-side helper: validators canonicalize each expanded name against
    ``npc_list``, so configs only ever store recorded source names.
    """
    group = alias_group_by_name(name)
    return list(group["members"]) if group else [str(name)]


def alias_search_entries(query: str) -> list[dict]:
    """Synthetic ``{id, name, icon_url, tracked}`` autocomplete rows for aliases
    matching ``query`` (substring, case-insensitive) — prepended to NPC search
    results so "winter…" surfaces "Wintertodt" as a first-class pick, rendering
    with the same fields as real NPC rows. ``tracked`` is always True: an alias
    only exists because an activity's loot is recorded under its member NPCs, so
    it is a real tracked source by construction."""
    q = str(query or "").strip().lower()
    if len(q) < 2:
        return []
    return [
        {
            "id": g["npc_id"],
            "name": g["name"],
            "icon_url": f"{_IMG_BASE}/npcdb/{g['npc_id']}.png",
            "tracked": True,
        }
        for g in NPC_SOURCE_ALIASES
        if q in g["name"].lower()
    ]
