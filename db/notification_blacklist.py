"""Group notification blacklist — the single matching rule (pipeline + web API).

A group leader blacklists an **item**, an **NPC** or a **region**; every Discord
notification that group would have received about that item, about anything from
that NPC, or about anything that happened in that place, is withheld. The
submission itself is untouched — it is still recorded, still scored, still on the
lootboard and the leaderboards. This is a noise control, not a data control.

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

Region keys come in two shapes, mirroring Dink's death filter and then going
past it. A **bare id** ("9043") mutes exactly one map region, which is all Dink
supports. An **area name** ("Castle Wars") mutes every region that area spans,
resolved through ``utils.region_names`` — so a leader picks a place from a list
instead of pasting the fourteen ids a raid occupies. Incoming payloads are
keyed by both their ``region_id`` and the name *the server* resolves that id
to, so a name entry still fires when the client sent no name, and a client
cannot dodge an entry by spelling the area differently.
"""
from __future__ import annotations

from utils import region_names
from utils.npc_names import npc_base_slug, npc_match_key, npc_slug

#: The kinds of thing a group can blacklist.
ENTRY_TYPES = ("item", "npc", "region")

#: How each entry type is described in a skip reason.
_TYPE_LABELS = {"item": "item", "npc": "NPC", "region": "region"}

#: Notification types the blacklist applies to: the per-submission announcements
#: that make up a clan's feed. Deliberately NOT event notifications — an event
#: tile completion is a curated, one-off result the group asked for, and muting
#: it because the tile happens to name a blacklisted item would delete
#: information rather than reduce noise. DM notifications are excluded
#: structurally instead: they carry no group_id, so no group's list can reach
#: a member's own submission DMs.
BLACKLISTABLE_TYPES = frozenset(
    {"drop", "clog", "pet", "pb", "ca", "death", "kc_milestone", "rank_milestone"}
)

#: Payload keys naming the ITEM a notification is about. ``pet_name`` is here
#: because a pet is an item ("Baby mole"): a leader who blacklists the pet
#: means the pet announcement.
ITEM_NAME_KEYS = ("item_name", "pet_name")

#: Payload keys naming the NPC/activity a notification came FROM. ``source`` is
#: the killer on a death and the pet's source boss; ``boss_name`` is the PB's.
NPC_NAME_KEYS = ("npc_name", "boss_name", "source")

#: Payload keys naming WHERE a notification happened. Only deaths carry these
#: today; ``region_name`` and ``location`` are the same area name, sent twice
#: by the plugin for different renderers.
REGION_NAME_KEYS = ("region_name", "location")

#: Payload key carrying the numeric map region.
REGION_ID_KEY = "region_id"

#: Prefix distinguishing a raw-region-id key from an area-name slug. ``npc_slug``
#: would reduce "9043" and the id 9043 to the same string, and they must not
#: collide: an area-name entry covers every region in the area, a raw id covers
#: exactly one.
_REGION_ID_PREFIX = "#"

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


def region_id_key(region_id) -> str:
    """The key identifying exactly one map region id."""
    try:
        return f"{_REGION_ID_PREFIX}{int(str(region_id).strip())}"
    except (TypeError, ValueError):
        return ""


def region_key(value) -> str:
    """Normalized identity for a blacklisted place.

    A bare number is one map region and covers only itself — Dink's whole
    region filter, kept for regions the name data does not cover. Anything else
    is an area name ("Castle Wars"), which covers *every* region the area spans;
    that is the part Dink cannot do, and the reason a leader can pick "Chambers
    of Xeric" instead of pasting fourteen ids.
    """
    text = str(value).strip() if value is not None else ""
    if text.startswith(_REGION_ID_PREFIX):
        text = text[1:].strip()
    if not text:
        return ""
    if text.isdigit():
        return region_id_key(text)
    key = region_names.region_key(text)
    return "" if key in _UNKNOWN_SOURCE_KEYS else key


def entry_key(entry_type: str, name) -> str:
    """The ``match_key`` stored for a blacklist row of this type."""
    if entry_type == "npc":
        return npc_key(name)
    if entry_type == "region":
        return region_key(name)
    return item_key(name)


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


def _region_lookup_keys(data) -> set[str]:
    """Keys an incoming payload's location should be tested against.

    Both the raw id and the *server's* name for that id, so an area-name entry
    still fires on a payload that carries only ``region_id`` — an older client,
    or a region the plugin could not name. Resolving the id here rather than
    trusting the payload's ``region_name`` also means a client cannot dodge a
    blacklist entry by sending a different spelling.
    """
    keys: set[str] = set()

    region_id = data.get(REGION_ID_KEY)
    id_key = region_id_key(region_id)
    if id_key:
        keys.add(id_key)
        resolved = region_names.name_for(region_id)
        if resolved:
            keys.add(region_names.region_key(resolved))

    for key in REGION_NAME_KEYS:
        slug = region_key(data.get(key))
        # A numeric name field would produce an id key; the id belongs to
        # REGION_ID_KEY alone, so ignore that reading here.
        if slug and not slug.startswith(_REGION_ID_PREFIX):
            keys.add(slug)

    return keys


def payload_subjects(data) -> dict[str, set[str]]:
    """The keys, per entry type, a notification payload could be muted on."""
    empty: dict[str, set[str]] = {entry_type: set() for entry_type in ENTRY_TYPES}
    if not isinstance(data, dict):
        return empty

    subjects = dict(empty)
    subjects["item"] = {k for k in (item_key(data.get(key)) for key in ITEM_NAME_KEYS) if k}
    for key in NPC_NAME_KEYS:
        subjects["npc"] |= _npc_lookup_keys(data.get(key))
    subjects["region"] = _region_lookup_keys(data)
    return subjects


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
    out: dict[str, set[str]] = {entry_type: set() for entry_type in ENTRY_TYPES}
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
    subjects = payload_subjects(data)
    if not any(subjects.values()):
        return None
    try:
        blacklist = load_group_blacklist(db_session, group_id)
    except Exception:
        return None
    for entry_type in ENTRY_TYPES:
        hit = subjects[entry_type] & blacklist[entry_type]
        if hit:
            return f"blacklisted {_TYPE_LABELS[entry_type]} '{sorted(hit)[0]}'"
    return None
