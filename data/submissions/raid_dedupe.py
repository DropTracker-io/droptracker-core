"""Content-level dedup for re-looted raid reward chests.

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

from utils.npc_names import npc_base_slug, npc_match_key

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


def _bundle_is_new(redis_key: str) -> bool:
    """Atomically record first sight of a bundle. Fails open."""
    try:
        from utils.redis import redis_client

        client = getattr(redis_client, "client", None)
        if client is None:
            return True
        return bool(client.set(redis_key, "1", nx=True, ex=RELOOT_TTL_SECONDS))
    except Exception:
        return True


def flag_raid_reloot_duplicates(processed_items) -> int:
    """Flag the raid drop embeds of an already-seen chest bundle.

    Call once per webhook payload, after ``process_webhook_data`` (which
    normalizes each embed's ``world_type``) and before dispatching embeds to
    processors. One payload carries at most one loot bundle, but embeds are
    grouped by (acc_hash, raid, world) defensively so a mixed payload can only
    ever flag its raid-sourced drops. Returns the number of embeds flagged.
    """
    bundles: dict[tuple, list] = {}
    for item in processed_items or []:
        if str(item.get("type") or "").strip().lower() not in _DROP_TYPES:
            continue
        base = _raid_base_key(item.get("source"))
        if base is None:
            continue
        acc_hash = item.get("acc_hash")
        if not acc_hash:
            continue
        world = str(item.get("world_type") or "main").strip().lower() or "main"
        bundles.setdefault((str(acc_hash), base, world), []).append(item)

    flagged = 0
    for (acc_hash, base, world), items in bundles.items():
        signature = ",".join(sorted(
            f"{item.get('item_id', item.get('id'))}:{item.get('quantity')}"
            for item in items
        ))
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
        redis_key = f"raidloot:reloot:{world}:{acc_hash}:{base}:{digest}"
        if not _bundle_is_new(redis_key):
            for item in items:
                item[RELOOT_FLAG] = True
            flagged += len(items)
    return flagged
