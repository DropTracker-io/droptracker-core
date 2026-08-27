"""``POST /state/sync`` — accept a full account state snapshot from the plugin.

The counterpart to the event submissions on ``/webhook``. Those say "this just
happened"; a snapshot says "this is everything, right now", which is the only
thing that can describe what a player already owned before they installed the
plugin.

Two properties this endpoint must hold:

* **Idempotent.** Every write is an upsert keyed by player, so replaying the
  same snapshot changes nothing and a client that could not tell whether its
  request landed may simply send it again.
* **Additive for collection log items.** A snapshot's item map is what that
  client has *seen*, which after a partial read is a subset of what the player
  owns. Rows are never deleted here; the authoritative slot totals travel
  separately in ``clog_slots``.

Derived notifications (new item, quest completed, ...) are computed but NOT
emitted yet — see ``_SYNC_EMIT_EVENTS``. Existing plugin handlers already
announce these live, so emitting from here before the dedupe against
``collection`` / ``combat_achievement`` rows is proven would double-notify every
user at once. The diff is logged so the shape can be checked against real
traffic first.
"""
import asyncio
import json
import os
import time
from datetime import datetime

from quart import Blueprint, jsonify, request

from api.core import get_db_session
from db.models import (
    Player,
    PlayerCollectionLogItem,
    PlayerCombatAchievementVarps,
    PlayerDiaryTier,
    PlayerQuestState,
    PlayerState,
)
from services.state_sync import (
    MAX_ITEMS,
    MAX_QUESTS,
    MAX_VARPS,
    count_completed_combat_achievements,
    deserialize_varps,
    improved_diary_tiers,
    is_late_collection_log_init,
    new_collection_log_items,
    newly_completed_quests,
    parse_diary_tiers,
    parse_int_map,
    parse_skills,
    repair_name_derived_items,
    serialize_varps,
    snapshot_summary,
)

state_sync_bp = Blueprint("state_sync", __name__)

# Off until the dedupe against existing event rows is verified against real
# traffic. Flipping this on without that is the one mistake here that would
# spam every user simultaneously.
_SYNC_EMIT_EVENTS = os.getenv("STATE_SYNC_EMIT_EVENTS", "false").lower() == "true"

# A snapshot is a few hundred KB at worst; anything larger is not a real client.
_MAX_BODY_BYTES = 2 * 1024 * 1024

# How many reported slots the structure cannot place are kept per snapshot.
# They are worth keeping — that is how a wrong id in the structure gets found
# and repaired — but they are also the one part of a snapshot the structure
# cannot bound, so they get an allowance rather than free rein. Accounts on a
# correct structure report a handful.
MAX_UNDEFINED_ITEMS = 200


@state_sync_bp.post("/state/sync")
async def state_sync():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    raw = await request.get_data()
    if len(raw) > _MAX_BODY_BYTES:
        return jsonify({"error": "Snapshot too large"}), 413

    try:
        snapshot = json.loads(raw)
    except ValueError:
        return jsonify({"error": "Malformed JSON"}), 400
    if not isinstance(snapshot, dict):
        return jsonify({"error": "Snapshot must be an object"}), 422

    player_name = snapshot.get("player_name")
    acc_hash = snapshot.get("acc_hash")
    if not acc_hash:
        return jsonify({"error": "acc_hash is required"}), 422

    try:
        result = await asyncio.to_thread(_apply_snapshot, player_name, str(acc_hash), snapshot)
    except Exception as exc:
        print(f"/state/sync failed: {exc}")
        return jsonify({"error": "Could not apply snapshot"}), 500

    if result is None:
        # Unknown account: identity is established by the normal submission
        # flow (which creates the Player row via WOM). Answering 202 keeps the
        # client quiet — there is nothing for it to retry or fix.
        return jsonify({"accepted": False, "reason": "unknown_player"}), 202

    return jsonify({"accepted": True, **result}), 200


# Reference data, so it is read once per process rather than per snapshot.
# Cached for a few minutes rather than forever: re-running the structure sync
# used to leave every long-lived API worker on the old slot set until it was
# restarted, which made a structure fix look like it had not worked.
_KNOWN_SLOTS_CACHE = None
_KNOWN_SLOTS_CACHED_AT = 0.0
_KNOWN_SLOTS_TTL_SECONDS = 300


def _known_collection_log_slots(db_session):
    """``(slot ids, name -> the one slot with that name)`` from the structure.

    The slot set is used to *report* how much of a snapshot the structure cannot
    place; nothing is filtered on it — see ``_apply_snapshot``. The name index
    is what repairs the plugin's name-derived unlock ids, and deliberately holds
    only names belonging to exactly one slot: twenty-three names (every Graceful
    piece, all the decorative armour) sit on several slots at once, and a name
    cannot say which of them a player unlocked.

    Both are empty when the structure is unknown.
    """
    global _KNOWN_SLOTS_CACHE, _KNOWN_SLOTS_CACHED_AT
    now = time.monotonic()
    if _KNOWN_SLOTS_CACHE is not None and now - _KNOWN_SLOTS_CACHED_AT < _KNOWN_SLOTS_TTL_SECONDS:
        return _KNOWN_SLOTS_CACHE

    from db.models import PluginManifestSection

    row = (
        db_session.query(PluginManifestSection)
        .filter(PluginManifestSection.key == "collection_log")
        .first()
    )
    slots = set()
    by_name = {}
    if row is not None:
        try:
            for tab in json.loads(row.payload):
                for page in tab.get("pages", []):
                    names = page.get("names") or []
                    for index, item_id in enumerate(page.get("items", [])):
                        if not isinstance(item_id, int):
                            continue
                        slots.add(item_id)
                        name = names[index] if index < len(names) else ""
                        if isinstance(name, str) and name.strip():
                            by_name.setdefault(name.strip().lower(), set()).add(item_id)
        except (TypeError, ValueError):
            slots, by_name = set(), {}

    unique_by_name = {name: ids.pop() for name, ids in by_name.items() if len(ids) == 1}

    _KNOWN_SLOTS_CACHE = (slots, unique_by_name)
    _KNOWN_SLOTS_CACHED_AT = now
    return _KNOWN_SLOTS_CACHE


def _name_lookup(db_session, item_ids):
    """Item id -> name, for the handful of ids the structure cannot place."""
    if not item_ids:
        return {}

    from db.models import ItemList

    return {
        row[0]: row[1]
        for row in db_session.query(ItemList.item_id, ItemList.item_name)
        .filter(ItemList.item_id.in_(list(item_ids)))
        .all()
        if row[1]
    }


def _resolve_player_id(db_session, player_name, acc_hash):
    """Hash-first identity, matching /notifications and /load_config."""
    player = db_session.query(Player).filter(Player.account_hash == acc_hash).first()
    if not player:
        player = (
            db_session.query(Player)
            .filter(Player.player_name == player_name, Player.account_hash == acc_hash)
            .first()
        )
    return player.player_id if player else None


def _apply_snapshot(player_name, acc_hash, snapshot):
    db_session = get_db_session()
    try:
        player_id = _resolve_player_id(db_session, player_name, acc_hash)
        if player_id is None:
            return None

        items = parse_int_map(snapshot.get("items"), limit=MAX_ITEMS, min_key=1, min_value=1)
        # Everything the client reports is stored, including ids the structure
        # does not define. This used to filter against the structure, on the
        # grounds that a client had once reported things that are not slots —
        # but back then the structure was scraped from the wiki and was *wrong*
        # about ~100 slots (variant ids: the log holds the uncharged Alchemist's
        # amulet, the empty Master scroll book), so the filter was throwing away
        # correct data from the game and leaving players' pages permanently
        # short. The structure now comes from the game cache and no longer
        # guesses, but the reasoning outlives that fix: a game update adds slots
        # before we ingest them, and the rendering side already ignores
        # undefined ids, so nothing reaches a collection log page that does not
        # belong on one. Keeping the rows means a structure refresh repairs
        # everyone who has already synced instead of only those who open their
        # log again afterwards.
        #
        # The structure is still what bounds the table, though: everything it
        # defines is kept, and only a small allowance of anything else, so a
        # client that reports five thousand invented ids cannot write five
        # thousand rows. Real accounts land two to four over.
        known_slots, slot_by_name = _known_collection_log_slots(db_session)
        undefined_items = 0
        repaired_items = 0
        if known_slots:
            repaired_items = repair_name_derived_items(
                items,
                known_slots,
                slot_by_name,
                _name_lookup(db_session, [i for i in items if i not in known_slots]),
            )
            undefined = [i for i in items if i not in known_slots]
            undefined_items = len(undefined)
            for item_id in undefined[MAX_UNDEFINED_ITEMS:]:
                del items[item_id]
        quests = parse_int_map(snapshot.get("quests"), limit=MAX_QUESTS, min_key=0, min_value=0)
        # min_value=None: a varp with its top bit set arrives as a negative
        # signed int, and dropping those would lose 32 completed tasks each.
        varps = parse_int_map(
            snapshot.get("ca_varps"), limit=MAX_VARPS, min_key=1, min_value=None
        )
        # Individually completed task varbits, when the client had a registry.
        raw_tasks = snapshot.get("ca_tasks")
        completed_tasks = []
        if isinstance(raw_tasks, list):
            for value in raw_tasks[:1000]:
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    completed_tasks.append(value)
        diary_tiers = parse_diary_tiers(snapshot.get("diary_tiers"))
        skills = parse_skills(snapshot.get("skills"))

        previous_items = {
            row.item_id: row.quantity
            for row in db_session.query(PlayerCollectionLogItem)
            .filter(PlayerCollectionLogItem.player_id == player_id)
            .all()
        }
        previous_quests = {
            row.quest_id: row.state
            for row in db_session.query(PlayerQuestState)
            .filter(PlayerQuestState.player_id == player_id)
            .all()
        }
        previous_diaries = {
            (row.area_id, row.tier): row.completed_count
            for row in db_session.query(PlayerDiaryTier)
            .filter(PlayerDiaryTier.player_id == player_id)
            .all()
        }

        # ── Diff before writing ──────────────────────────────────────────────
        fresh_items = new_collection_log_items(previous_items, items)
        late_init = is_late_collection_log_init(len(previous_items), len(fresh_items))
        finished_quests = newly_completed_quests(previous_quests, quests)
        improved_diaries = improved_diary_tiers(previous_diaries, diary_tiers)

        # ── Persist ──────────────────────────────────────────────────────────
        _upsert_state(db_session, player_id, snapshot, skills)
        _upsert_items(db_session, player_id, previous_items, items)
        _upsert_quests(db_session, player_id, previous_quests, quests)
        _upsert_diaries(db_session, player_id, previous_diaries, diary_tiers)
        _upsert_combat_achievements(db_session, player_id, varps, completed_tasks)

        db_session.commit()

        diff = {
            "new_items": len(fresh_items),
            "quests_completed": len(finished_quests),
            "diary_tiers_improved": len(improved_diaries),
            "late_clog_init": late_init,
            # Slots the game reported that the structure cannot place. A steady
            # non-zero figure here is the structure being wrong, not the client:
            # see scripts.sync_collection_log --audit.
            "undefined_items": undefined_items,
            # Name-derived unlock ids moved onto the slot they meant; zero once
            # clients resolve them against the log's own slots.
            "repaired_items": repaired_items,
        }
        print(
            f"/state/sync player={player_id} "
            f"{snapshot_summary(snapshot)} diff={diff} emit={_SYNC_EMIT_EVENTS}"
        )

        return {"stored": {"items": len(items), "quests": len(quests)}, "diff": diff}
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()


def _upsert_state(db_session, player_id, snapshot, skills):
    row = db_session.query(PlayerState).filter(PlayerState.player_id == player_id).first()
    if row is None:
        row = PlayerState(player_id=player_id)
        db_session.add(row)

    account_type = snapshot.get("account_type")
    combat_level = snapshot.get("combat_level")
    if isinstance(account_type, int):
        row.account_type = account_type
    if isinstance(combat_level, int):
        row.combat_level = combat_level

    clog_slots = snapshot.get("clog_slots")
    clog_total = snapshot.get("clog_slots_total")
    # Only trust the counters when the game actually reported them; the client
    # sends nothing when the varps were unreadable, and a zero here would look
    # like the player lost their whole collection log.
    if isinstance(clog_slots, int) and isinstance(clog_total, int) and clog_total > 0:
        row.clog_slots = clog_slots
        row.clog_slots_total = clog_total

    manifest_version = snapshot.get("manifest_version")
    if isinstance(manifest_version, str):
        row.manifest_version = manifest_version[:32]

    source = snapshot.get("source")
    if isinstance(source, str):
        row.last_sync_source = source[:32]

    row.last_synced_at = datetime.now()

    if skills:
        _update_player_experience(db_session, player_id, skills)


def _update_player_experience(db_session, player_id, skills):
    """Refresh the existing latest-only per-skill XP row.

    Reuses ``player_exp`` rather than adding a second XP store. Per-skill
    *history* is intentionally still absent: it is the one unbounded table in
    this design and wants a retention policy first.
    """
    from db.models import PlayerExperience

    row = (
        db_session.query(PlayerExperience)
        .filter(PlayerExperience.player_id == player_id)
        .first()
    )
    if row is None:
        row = PlayerExperience(player_id=player_id)
        db_session.add(row)

    for name, xp in skills.items():
        column = name.strip().lower()
        # Only assign to real columns — a snapshot naming a skill we do not have
        # a column for (a new skill, or a hostile client) must not create
        # attributes on the ORM object.
        if hasattr(PlayerExperience, column) and column not in ("id", "player_id", "last_updated"):
            setattr(row, column, xp)
    row.last_updated = datetime.now()


def _upsert_items(db_session, player_id, previous, incoming):
    """Additive: writes new and changed slots, never removes.

    A partial read is a subset, so treating absence as removal would delete a
    player's collection log the first time they synced without opening it.
    """
    for item_id, quantity in incoming.items():
        before = previous.get(item_id)
        if before == quantity:
            continue
        if before is None:
            db_session.add(
                PlayerCollectionLogItem(
                    player_id=player_id, item_id=item_id, quantity=quantity
                )
            )
        else:
            (
                db_session.query(PlayerCollectionLogItem)
                .filter(
                    PlayerCollectionLogItem.player_id == player_id,
                    PlayerCollectionLogItem.item_id == item_id,
                )
                .update({"quantity": quantity})
            )


def _upsert_quests(db_session, player_id, previous, incoming):
    for quest_id, state in incoming.items():
        before = previous.get(quest_id)
        if before == state:
            continue
        if before is None:
            db_session.add(
                PlayerQuestState(player_id=player_id, quest_id=quest_id, state=state)
            )
        else:
            (
                db_session.query(PlayerQuestState)
                .filter(
                    PlayerQuestState.player_id == player_id,
                    PlayerQuestState.quest_id == quest_id,
                )
                .update({"state": state})
            )


def _upsert_diaries(db_session, player_id, previous, incoming):
    for area_id, tier, completed in incoming:
        before = previous.get((area_id, tier))
        if before == completed:
            continue
        if before is None:
            db_session.add(
                PlayerDiaryTier(
                    player_id=player_id,
                    area_id=area_id,
                    tier=tier,
                    completed_count=completed,
                )
            )
        else:
            (
                db_session.query(PlayerDiaryTier)
                .filter(
                    PlayerDiaryTier.player_id == player_id,
                    PlayerDiaryTier.area_id == area_id,
                    PlayerDiaryTier.tier == tier,
                )
                .update({"completed_count": completed})
            )


def _upsert_combat_achievements(db_session, player_id, varps, completed_tasks=None):
    """Stores the raw completion bits plus a task count.

    Skipped entirely when the client sent no varps — that means it had no
    manifest, not that the player completed nothing, and overwriting real data
    with an empty set would look like mass un-completion.
    """
    if not varps and not completed_tasks:
        return

    row = (
        db_session.query(PlayerCombatAchievementVarps)
        .filter(PlayerCombatAchievementVarps.player_id == player_id)
        .first()
    )
    if row is None:
        row = PlayerCombatAchievementVarps(player_id=player_id, varps="{}")
        db_session.add(row)

    merged = deserialize_varps(row.varps)
    merged.update(varps)

    if varps:
        row.varps = serialize_varps(merged)
        row.tasks_completed = count_completed_combat_achievements(merged)

    # The per-task list is replaced wholesale rather than merged: it is a
    # complete read of the registry, so an absent task means "not completed",
    # not "not seen". Merging would make an un-completed task impossible.
    if completed_tasks is not None and completed_tasks:
        row.completed_tasks = json.dumps(sorted(set(completed_tasks)), separators=(",", ":"))
