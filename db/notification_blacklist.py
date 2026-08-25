"""Group notification blacklist — the single matching rule (pipeline + web API).

A group leader blacklists an **item** or an **NPC**; every Discord notification
that group would have received about that item, or about anything from that
NPC, is withheld. The submission itself is untouched — it is still recorded,
still scored, still on the lootboard and the leaderboards. This is a noise
control, not a data control.

Both sides of the feature import from here so they cannot drift apart:

  * ``data/submissions/common.create_notification`` (enqueue) and
    ``services/notification_service`` (send) match queued payloads with
    :func:`blacklist_reason`;
  * ``web_api/routes/group_blacklist.py`` writes rows with :func:`entry_key`,
    so what a leader adds is normalized exactly the way the pipeline will
    later look it up.

**Names, not ids.** The payloads the processors build carry names
(``item_name`` / ``npc_name`` / ``boss_name`` / ``source``) and only sometimes
an id, and the same physical item arrives under several ids (noted / stacked /
cosmetic variants). Matching on the normalized name is the only rule that works
for every submission type; ``game_id`` on the row exists for the picker's icon.

NPC keys go through ``utils.npc_names.npc_match_key`` — the codebase's one
spelling/article/alias-insensitive NPC identity — plus a base-raid fold, so
blacklisting "Chambers of Xeric" also silences its Challenge Mode. The fold is
deliberately one-way: blacklisting *only* Challenge Mode leaves normal CoX
notifications alone.
"""
from __future__ import annotations

from utils.npc_names import npc_base_slug, npc_match_key, npc_slug

#: The two kinds of thing a group can blacklist.
ENTRY_TYPES = ("item", "npc")

#: Notification types the blacklist applies to: the per-submission announcements
#: that make up a clan's feed. Deliberately NOT event notifications — an event
#: tile completion is a curated, one-off result the group asked for, and muting
#: it because the tile happens to name a blacklisted item would delete
#: information rather than reduce noise. DM notifications are excluded
#: structurally instead: they carry no group_id, so no group's list can reach
#: a member's own submission DMs.
BLACKLISTABLE_TYPES = frozenset({"drop", "clog", "pet", "pb", "ca", "death"})

#: Payload keys naming the ITEM a notification is about. ``pet_name`` is here
#: because a pet is an item ("Baby mole"): a leader who blacklists the pet
#: means the pet announcement.
ITEM_NAME_KEYS = ("item_name", "pet_name")

#: Payload keys naming the NPC/activity a notification came FROM. ``source`` is
#: the killer on a death and the pet's source boss; ``boss_name`` is the PB's.
NPC_NAME_KEYS = ("npc_name", "boss_name", "source")

#: Source names that mean "we don't know", never a real NPC. Matching these
#: would let one blacklist entry mute unrelated submissions.
_UNKNOWN_SOURCE_KEYS = frozenset({"unknown", "none", "null", "n-a", "na", ""})


def item_key(name) -> str:
    """Normalized identity for an item name.

    Case-, punctuation- and separator-insensitive, so the plugin's
    ``Twisted_bow``, the catalog's ``Twisted bow`` and a leader typing
    ``twisted bow`` are one key.
    """
    return npc_slug(name)


def npc_key(name) -> str:
    """Normalized identity for an NPC/activity name (see ``npc_match_key``)."""
    key = npc_match_key(name)
    return "" if key in _UNKNOWN_SOURCE_KEYS else key


def entry_key(entry_type: str, name) -> str:
    """The ``match_key`` stored for a blacklist row of this type."""
    return npc_key(name) if entry_type == "npc" else item_key(name)


def _npc_lookup_keys(name) -> set[str]:
    """Keys an incoming NPC name should be tested against.

    Its own match key, plus the base raid when it is a mode variant — that is
    what makes a "Chambers of Xeric" entry cover Challenge Mode without a
    Challenge Mode entry covering normal raids.
    """
    key = npc_key(name)
    if not key:
        return set()
    keys = {key}
    base = npc_base_slug(key)
    if base:
        keys.add(base)
    return keys


def payload_subjects(data) -> tuple[set[str], set[str]]:
    """(item keys, npc keys) a notification payload could be blacklisted on."""
    if not isinstance(data, dict):
        return set(), set()
    items = {k for k in (item_key(data.get(key)) for key in ITEM_NAME_KEYS) if k}
    npcs: set[str] = set()
    for key in NPC_NAME_KEYS:
        npcs |= _npc_lookup_keys(data.get(key))
    return items, npcs


def load_group_blacklist(db_session, group_id) -> dict[str, set[str]]:
    """This group's blacklist as ``{"item": {keys}, "npc": {keys}}``.

    Raises nothing the caller has to handle beyond the DB error itself; both
    call sites treat a failure as "not blacklisted" (fail open).
    """
    # Imported here, not at module scope: this module is loaded by the
    # submission processors and the web API alike, and only the query needs the
    # ORM. (It is also the form the unit-test bootstrap can stub — see
    # tests/conftest.py.)
    from db.models import GroupNotificationBlacklist

    rows = (
        db_session.query(
            GroupNotificationBlacklist.entry_type,
            GroupNotificationBlacklist.match_key,
        )
        .filter(GroupNotificationBlacklist.group_id == group_id)
        .all()
    )
    out: dict[str, set[str]] = {"item": set(), "npc": set()}
    for entry_type, key in rows:
        if key and entry_type in out:
            out[entry_type].add(key)
    return out


def blacklist_reason(db_session, group_id, notification_type, data) -> str | None:
    """Why this group must not be sent this notification, or ``None``.

    The return value is a short human-readable reason ("blacklisted item
    'twisted-bow'") so the skip is greppable in the logs and legible on a
    ``notification_queue`` row, rather than a bare boolean nobody can debug.

    Fails **open**: no group_id, an out-of-scope type, no payload, an empty
    list or a DB fault all return ``None``. A transient database problem must
    never silently mute every group's Discord.
    """
    if not group_id or notification_type not in BLACKLISTABLE_TYPES:
        return None
    items, npcs = payload_subjects(data)
    if not items and not npcs:
        return None
    try:
        blacklist = load_group_blacklist(db_session, group_id)
    except Exception:
        return None
    hit = items & blacklist["item"]
    if hit:
        return f"blacklisted item '{sorted(hit)[0]}'"
    hit = npcs & blacklist["npc"]
    if hit:
        return f"blacklisted NPC '{sorted(hit)[0]}'"
    return None
