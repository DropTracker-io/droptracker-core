"""Content-level dedup for loot bundles that reach intake more than once.

Two unrelated client behaviours produce the same shape of bug — ONE loot event
submitted twice, each copy carrying its own freshly-minted GUID, so every
GUID-keyed defence we have (``ensure_can_create``, and the events ledger's
``(task, team, submission_guid)`` unique index) is blind to it. Both are caught
here, before dispatch, by fingerprinting the payload's drop bundle:

* :func:`flag_raid_reloot_duplicates` — a raid reward chest re-opened at the
  bank collection chest, minutes or hours later. Fingerprints the whole
  bundle, because a re-opened chest replays the chest's full contents, and
  reads the kill count to tell that apart from a later completion that
  rolled the same bundle.
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
Plugin builds from 5.4.0 suppress the repeat client-side
(RaidLootDeduplicator); this layer catches clients restarted between the two
chest opens, and older builds still in the wild. GUID dedup cannot help: the
second loot event is a fresh submission with a fresh GUID. So the payload's
raid drop embeds are fingerprinted by content instead — acc_hash + base raid +
world type + sorted (item id, quantity) pairs — and remembered in Redis for
``RELOOT_TTL_SECONDS``. An identical bundle seen again inside the window is
flagged; the intake dispatchers reject flagged embeds instead of processing
them (no Drop row, no leaderboard GP, no events-engine credit).

An identical bundle is not proof of a re-open. A purple chest holds the unique
and nothing else, so two completions that roll the same unique produce the
same one-item bundle, and this layer used to reject the second as a re-open:
f Davy's back-to-back Ancestral hats (2026-08-17) and Yo Default's Avernic
defender hilts (ticket #441, 2026-09-16) were real completions whose drops
never recorded. That is the highest-value loot there is, not the "one skipped
common-loot bundle" this used to accept as the cost.

The kill count separates the two cases. A later completion reports a higher
count. A restarted client re-reads the stored count, so its re-open reports
the SAME count as the original chest. Each accepted chest therefore records
its kill count as a per-account watermark for its raid MODE, and a repeated
bundle whose count is past the watermark is a new completion: it is accepted,
and re-armed so its own re-open is still caught. That matches the plugin's
rule, where the next completion message re-arms the client-side dedup.

The watermark is per mode because each mode has its own kill counter, while
the fingerprint folds modes because a restarted client names the base raid.
Anything the kill count cannot settle keeps the old verdict and is flagged:
an unknown count, no watermark for that mode, or a build older than
``RELOOT_KC_TRUSTED_FROM``, where a re-open bumps the cached count and
arrives claiming kc+1.
Redis trouble fails open (bundle treated as new)."""

import hashlib
import logging
import re

from utils.npc_names import (
    canonical_encounter_name,
    is_multi_path_loot_source,
    npc_base_slug,
    npc_match_key,
)

log = logging.getLogger(__name__)

#: Base-raid match keys whose reward chests can be re-opened at a bank chest.
RAID_BASE_KEYS = {"theatre-of-blood", "tombs-of-amascut", "chambers-of-xeric"}

RELOOT_TTL_SECONDS = 2 * 60 * 60

#: First plugin build whose kill count can tell a re-opened chest from the next
#: completion. Its RaidLootDeduplicator keeps a suppressed re-open from bumping
#: the cached count, and after a restart the count is re-read from storage.
#: Earlier builds count every chest open as a kill.
RELOOT_KC_TRUSTED_FROM = (5, 4, 0)

#: Lifetime of a raid mode's completion watermark. A back-to-back raid only
#: needs it to outlast one fingerprint, but a player switching modes compares
#: against the watermark of a mode they may not have played for weeks.
RELOOT_KC_TTL_SECONDS = 30 * 24 * 60 * 60

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


def _redis():
    """The raw Redis client, or None when there isn't one."""
    from utils.redis import redis_client

    return getattr(redis_client, "client", None)


def _bundle_is_new(redis_key: str, ttl: int = RELOOT_TTL_SECONDS) -> bool:
    """Atomically record first sight of a bundle. Fails open."""
    try:
        client = _redis()
        if client is None:
            return True
        return bool(client.set(redis_key, "1", nx=True, ex=ttl))
    except Exception:
        return True


def _raid_bundles(processed_items) -> dict[tuple, list]:
    """A payload's raid drop embeds, grouped by (account, base raid, world).

    One payload carries one loot event, but embeds are grouped defensively so
    a mixed payload can only ever flag the drops belonging to a raid.
    """
    bundles: dict[tuple, list] = {}
    for item in processed_items or []:
        if str(item.get("type") or "").strip().lower() not in _DROP_TYPES:
            continue
        raid = _raid_base_key(item.get("source"))
        if raid is None:
            continue
        acc_hash = item.get("acc_hash")
        if not acc_hash:
            continue
        world = str(item.get("world_type") or "main").strip().lower() or "main"
        bundles.setdefault((str(acc_hash), raid, world), []).append(item)
    return bundles


def _bundle_signature(items) -> str:
    return ",".join(sorted(
        f"{item.get('item_id', item.get('id'))}:{item.get('quantity')}"
        for item in items
    ))


def _bundle_digest(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]


def _plugin_version(item) -> tuple[int, int, int] | None:
    """The submitting build's version from the ``p_v`` embed field
    ("6.0.6", "6.0.6-SNAPSHOT"), or None when it is missing or unreadable."""
    match = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", str(item.get("p_v") or ""))
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _trusted_kill_count(items) -> int | None:
    """The bundle's kill count, or None when it cannot tell a re-opened chest
    from the next completion: an old or unknown build, an unknown count
    (the plugin sends 0), or embeds that disagree."""
    counts = set()
    for item in items:
        version = _plugin_version(item)
        if version is None or version < RELOOT_KC_TRUSTED_FROM:
            return None
        try:
            kill_count = int(item.get("kill_count", item.get("killcount")))
        except (TypeError, ValueError):
            return None
        if kill_count <= 0:
            return None
        counts.add(kill_count)
    return counts.pop() if len(counts) == 1 else None


def _completion_key(acc_hash, world, items) -> str | None:
    """Redis key of the bundle's raid-mode watermark, or None when the embeds
    don't agree on one mode."""
    modes = {npc_match_key(item.get("source")) for item in items}
    if len(modes) != 1 or not next(iter(modes)):
        return None
    return f"raidloot:kc:{world}:{acc_hash}:{modes.pop()}"


def _is_later_completion(completion_key: str, kill_count: int) -> bool:
    """Whether ``kill_count`` is past the last accepted completion of this
    raid mode. No watermark is not "later": a repeated bundle is then as
    likely to be the re-opened chest. Fails open, like _bundle_is_new."""
    try:
        client = _redis()
        if client is None:
            return True
        last = client.get(completion_key)
    except Exception:
        return True
    try:
        return last is not None and kill_count > int(last)
    except (TypeError, ValueError):
        return False


def _remember(redis_key: str, value, ttl: int) -> None:
    """Best-effort SET with expiry; a failure only costs a later dedup."""
    try:
        client = _redis()
        if client is not None:
            client.set(redis_key, value, ex=ttl)
    except Exception:
        pass


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

    A bundle already seen inside the window is still accepted when its kill
    count is past its raid mode's watermark: that is the next completion
    rolling the same loot (see the module docstring), not a re-open.
    """
    flagged = 0
    for (acc_hash, raid, world), items in _raid_bundles(processed_items).items():
        signature = _bundle_signature(items)
        fingerprint = f"raidloot:reloot:{world}:{acc_hash}:{raid}:{_bundle_digest(signature)}"
        kill_count = _trusted_kill_count(items)
        completion_key = (
            _completion_key(acc_hash, world, items) if kill_count is not None else None
        )
        if not _bundle_is_new(fingerprint):
            # Who and what, so a "my drop was blocked" ticket is one journal
            # grep rather than an archaeology dig through kill-count gaps.
            detail = (items[0].get("player_name") or items[0].get("player"),
                      acc_hash, items[0].get("source"), world, kill_count, signature)
            if not (completion_key and _is_later_completion(completion_key, kill_count)):
                for item in items:
                    item[RELOOT_FLAG] = True
                flagged += len(items)
                log.info("Raid re-loot duplicate rejected: player=%r acc=%s "
                         "source=%r world=%s kc=%s items=%s", *detail)
                continue
            # A later completion that rolled the same bundle. Re-arm for THIS
            # completion, so its own re-open is caught for the full window
            # rather than whatever the first sighting had left.
            _remember(fingerprint, "1", RELOOT_TTL_SECONDS)
            log.info("Raid bundle repeat accepted as a later completion: player=%r "
                     "acc=%s source=%r world=%s kc=%s items=%s", *detail)
        if completion_key:
            _remember(completion_key, kill_count, RELOOT_KC_TTL_SECONDS)
    return flagged
