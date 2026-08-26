"""Content-level dedup for loot bundles that reach intake more than once.

Two unrelated client behaviours produce the same shape of bug — ONE loot event
submitted twice, each copy carrying its own freshly-minted GUID, so every
GUID-keyed defence we have (``ensure_can_create``, and the events ledger's
``(task, team, submission_guid)`` unique index) is blind to it. Both are caught
here, before dispatch, by fingerprinting the payload's drop bundle:

* :func:`flag_raid_reloot_duplicates` — a raid reward chest re-opened at the
  bank collection chest, minutes or hours later. Fingerprints the whole
  bundle, because a re-opened chest replays the chest's full contents.
* :func:`flag_multipath_loot_duplicates` — a multi-part boss whose single kill
  RuneLite delivers through more than one loot event, in the same tick.
  Fingerprints each ITEM, because those two events disagree on the item list
  (one carries the encounter's always-drop, the other omits it) and so never
  produce a matching whole-bundle hash.

Re-looted raid reward chests
----------------------------
A raid reward chest fires the plugin's loot event once when opened in the loot
room and again if the unclaimed loot is taken from the collection chest at the
bank — two identical drop bundles for ONE completion, minutes or hours apart.
Plugin builds after 5.4.0 suppress the repeat client-side
(RaidLootDeduplicator); this layer catches older builds still in the wild and
clients restarted between the two chest opens. GUID dedup cannot help: the
second loot event is a fresh submission with a fresh GUID. So the payload's
raid drop embeds are fingerprinted by content instead — acc_hash + base raid +
world type + sorted (item id, quantity) pairs — and remembered in Redis for
``RELOOT_TTL_SECONDS``. An identical bundle seen again inside the window is
flagged; the intake dispatchers reject flagged embeds instead of processing
them (no Drop row, no leaderboard GP, no events-engine credit).

False-positive risk — two completions of the same raid rolling a byte-identical
full bundle inside the window — is accepted as negligible: raid loot quantities
are roll- and points-scaled, and the cost is one skipped common-loot bundle.
Redis trouble fails open (bundle treated as new)."""

import hashlib

from utils.npc_names import (
    canonical_encounter_name,
    is_multi_path_loot_source,
    npc_base_slug,
    npc_match_key,
)

#: Base-raid match keys whose reward chests can be re-opened at a bank chest.
RAID_BASE_KEYS = {"theatre-of-blood", "tombs-of-amascut", "chambers-of-xeric"}

RELOOT_TTL_SECONDS = 2 * 60 * 60

#: Stamped onto each flagged embed dict; dispatchers must check it before
#: calling drop_processor.
RELOOT_FLAG = "raid_reloot_duplicate"

RELOOT_REJECT_MESSAGE = (
    "Duplicate raid loot: this reward chest bundle was already submitted for "
    "this completion (re-opened at the bank chest)."
)

#: One kill of a multi-part boss reaches intake through more than one RuneLite
#: loot event, fired in the SAME tick, so the window only has to outlive the
#: gap between the two copies — they are enqueued in the same second and drain
#: adjacently even under a backlog, which makes that gap near-zero regardless
#: of absolute queue lag.
#:
#: Now that matching is per ITEM rather than per bundle, this bound is load
#: bearing rather than nominal. A whole bundle repeating by chance is
#: vanishingly unlikely; a single common item repeating at the same quantity
#: on a genuine next kill is not — Araxxor, the quickest of these encounters
#: to re-kill at ~25s, would do it with any staple drop. So the window sits
#: below that re-kill time instead of above it, where 60s left it.
MULTIPATH_TTL_SECONDS = 20

MULTIPATH_FLAG = "multipath_loot_duplicate"

MULTIPATH_REJECT_MESSAGE = (
    "Duplicate boss loot: this kill was already submitted through another "
    "loot event (multi-part boss on an outdated plugin build)."
)

#: Embed ``type`` values that reach drop_processor (webhook.py aliases).
_DROP_TYPES = ("drop", "npc", "other")


def _raid_base_key(source) -> str | None:
    """Base-raid match key for a drop source, or None when it isn't a raid.

    Mode variants fold to the base raid ("Theatre of Blood: Hard Mode" and
    "Theatre of Blood" name the same chest).
    """
    key = npc_match_key(source)
    if not key:
        return None
    base = npc_base_slug(key) or key
    return base if base in RAID_BASE_KEYS else None


def _multipath_source_key(source) -> str | None:
    """Canonical encounter name for a multi-path boss source, else None.

    Sub-NPC names fold to the encounter first ("Dusk" -> "Grotesque
    Guardians"), which is the whole point: the two loot events name the kill
    differently, so only the canonical form groups them together.
    """
    if not is_multi_path_loot_source(source):
        return None
    return npc_match_key(canonical_encounter_name(source)) or None


def _bundle_is_new(redis_key: str, ttl: int = RELOOT_TTL_SECONDS) -> bool:
    """Atomically record first sight of a bundle. Fails open."""
    try:
        from utils.redis import redis_client

        client = getattr(redis_client, "client", None)
        if client is None:
            return True
        return bool(client.set(redis_key, "1", nx=True, ex=ttl))
    except Exception:
        return True


def _flag_duplicate_bundles(processed_items, source_key_fn, key_prefix, ttl, flag) -> int:
    """Fingerprint each (account, source, world) drop bundle in a payload and
    flag the ones already seen inside ``ttl``. Returns the number flagged.

    Shared by both dedup passes — they differ only in which sources are
    eligible (``source_key_fn`` returns None to skip) and how long a bundle is
    remembered. Embeds are grouped defensively so a mixed payload can only ever
    flag the drops belonging to an eligible source.
    """
    bundles: dict[tuple, list] = {}
    for item in processed_items or []:
        if str(item.get("type") or "").strip().lower() not in _DROP_TYPES:
            continue
        source_key = source_key_fn(item.get("source"))
        if source_key is None:
            continue
        acc_hash = item.get("acc_hash")
        if not acc_hash:
            continue
        world = str(item.get("world_type") or "main").strip().lower() or "main"
        bundles.setdefault((str(acc_hash), source_key, world), []).append(item)

    flagged = 0
    for (acc_hash, source_key, world), items in bundles.items():
        signature = ",".join(sorted(
            f"{item.get('item_id', item.get('id'))}:{item.get('quantity')}"
            for item in items
        ))
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
        redis_key = f"{key_prefix}:{world}:{acc_hash}:{source_key}:{digest}"
        if not _bundle_is_new(redis_key, ttl):
            for item in items:
                item[flag] = True
            flagged += len(items)
    return flagged


def flag_multipath_loot_duplicates(processed_items) -> int:
    """Flag the drop embeds of a multi-part boss kill already seen moments ago.

    RuneLite delivers one kill of these bosses through more than one loot
    event: for the Grotesque Guardians it fires ``NpcLootReceived`` naming
    **Dusk** (the guardian that drops the loot) *and* ``LootReceived`` naming
    the encounter. Plugin v5.4.0 suppresses the repeat client-side, but the
    plugin-hub pin sat on v5.3.0 from 2026-03-29 to 2026-08-03 and clients
    update on their own schedule — so between those dates every GG kill was
    recorded twice, inflating loot totals and scoring event tasks twice (a
    Renatus bingo player's Granite ring paid 4 points twice on 2026-08-03).

    Fingerprints each ITEM, not the bundle. The two loot events do not agree on
    the item LIST: RuneLite's encounter path carries the always-drop
    (Granite dust for the Guardians) that its NPC path omits, so one kill
    arrives as, say, {Runite bar x5, Granite hammer x1} and
    {Granite dust x99, Runite bar x5, Granite hammer x1}. Those bundles hash
    differently, which is why whole-bundle fingerprinting suppressed nothing
    and Shiny Quag's Granite hammer was still recorded twice on 2026-08-26 —
    ~300 duplicate rows a day across these encounters. Per item, the overlap
    is caught and the item unique to one path still lands exactly once.

    Safe because it is scoped to ``MULTI_PATH_LOOT_SOURCES``: those encounters
    cannot be re-killed inside the window, so the same item and quantity from
    the same account inside it is always one kill arriving twice. Ordinary NPCs
    are excluded precisely because they CAN be legitimately multi-killed in one
    tick with identical loot (AoE slayer routinely does it), and must never be
    suppressed.

    Call once per payload, after ``process_webhook_data`` and before
    dispatching embeds to processors. Returns the number of embeds flagged.
    """
    flagged = 0
    for item in processed_items or []:
        if str(item.get("type") or "").strip().lower() not in _DROP_TYPES:
            continue
        # Both spellings: drop_processor reads `source` OR `npc_name`
        # (data/submissions/drop.py), so a payload naming the boss the second
        # way must not slip past the dedup that names it the first.
        source_key = _multipath_source_key(
            item.get("source") or item.get("npc_name"))
        if source_key is None:
            continue
        acc_hash = item.get("acc_hash")
        if not acc_hash:
            continue
        world = str(item.get("world_type") or "main").strip().lower() or "main"
        signature = f"{item.get('item_id', item.get('id'))}:{item.get('quantity')}"
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
        redis_key = (
            f"bossloot:multipath:{world}:{acc_hash}:{source_key}:{digest}"
        )
        if not _bundle_is_new(redis_key, MULTIPATH_TTL_SECONDS):
            item[MULTIPATH_FLAG] = True
            flagged += 1
    return flagged


def duplicate_reject_message(item) -> str | None:
    """The reject message for an embed flagged by any content-dedup pass, or
    None when it is clean. Keeps the intake call sites to a single check as
    more passes are added."""
    if item.get(RELOOT_FLAG):
        return RELOOT_REJECT_MESSAGE
    if item.get(MULTIPATH_FLAG):
        return MULTIPATH_REJECT_MESSAGE
    return None


def flag_raid_reloot_duplicates(processed_items) -> int:
    """Flag the raid drop embeds of an already-seen chest bundle.

    Call once per webhook payload, after ``process_webhook_data`` (which
    normalizes each embed's ``world_type``) and before dispatching embeds to
    processors. One payload carries at most one loot bundle, but embeds are
    grouped by (acc_hash, raid, world) defensively so a mixed payload can only
    ever flag its raid-sourced drops. Returns the number of embeds flagged.
    """
    return _flag_duplicate_bundles(
        processed_items, _raid_base_key,
        "raidloot:reloot", RELOOT_TTL_SECONDS, RELOOT_FLAG)
