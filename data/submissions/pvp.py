"""PvP kills: one player killing another and taking their loot.

The plugin reports a player kill the way it reports an NPC kill: a ``drop``
payload with one embed per item, each with ``source_type="pvp"`` and the
victim's name in ``source``. Left alone, that bundle ran through the NPC
pipeline, which looked the victim up as an NPC, failed (queueing a
``new_npc`` notification and a wiki lookup per kill) and rejected it, or,
when the victim shared a name with a real NPC, booked it as that NPC's drop.

``collapse_pvp_bundle`` lifts those embeds out of the payload before routing
and folds them into one ``pvp_kill`` submission, so a kill is one row with its
items beside it rather than N drops. Every intake transport calls it (the
sync API route and queue consumer through ``process_webhook_data``, the
legacy webhook reader directly), and ``dispatch`` routes the result to
``pvp_kill_processor`` here.

Counting rules, in the order they apply:

* **Nowhere** (``PvpKill.excluded_reason``): kills inside a PvP minigame
  (``utils.death_regions.PVP_MINIGAME_REGIONS``), and kills of an account the
  killer's own user also owns (moving loot between your own accounts).
* **Per group** (``PvpKillGroup``): a kill of a fellow member does not count
  unless the group turned ``pvp_count_member_kills`` on, and only the first
  ``pvp_same_victim_daily_limit`` kills of one victim a day count (3 unless
  the group changed it, or ``pvp_same_victim_unlimited``).

Deaths to another player are already ``player_deaths`` rows (killer_type
"player"); ``record_pvp_death`` adds them to the PvP standings and posts them
for groups that asked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from utils.partitions import month_token
from utils.valuation_guard import exceeds_value_sanity_limit, sanitize_quantity

from .common import (
    SubmissionResponse,
    create_notification,
    debug_print,
    ensure_item_for_drop,
    ensure_player_and_auth,
    get_player_groups_with_global,
    get_true_item_value,
    received_at,
    resolve_attachment_from_drop_data,
    screenshot_gate_notice,
    screenshot_required,
    select_session_and_flag,
)

GLOBAL_GROUP_ID = 2

#: The submission type a collapsed kill travels as.
PVP_KILL_TYPE = "pvp_kill"

# Group settings (web_api/config_registry.py, "pvp" category).
CHANNEL_KEY = "channel_id_to_post_pvp"
MIN_VALUE_KEY = "pvp_minimum_value_to_notify"
COUNT_MEMBER_KILLS_KEY = "pvp_count_member_kills"
SAME_VICTIM_LIMIT_KEY = "pvp_same_victim_daily_limit"
SAME_VICTIM_UNLIMITED_KEY = "pvp_same_victim_unlimited"
NOTIFY_DEATHS_KEY = "pvp_notify_deaths"
DEATH_MIN_VALUE_KEY = "pvp_death_minimum_value"

DEFAULT_MIN_VALUE = 1_000_000
DEFAULT_SAME_VICTIM_LIMIT = 3
DEFAULT_DEATH_MIN_VALUE = 0

#: Items listed in a notification's {pvp_items} (the rest are summed).
NOTIFY_ITEM_LINES = 6

_DROP_TYPES = frozenset({"drop", "npc", "other"})

# --------------------------------------------------------------------------- #
# Loot keys (web148a, ticket 498)
#
# With loot keys on in game, a kill drops only bones (plus the key when the
# killer's inventory is full), so the kill arrives as a bones-only pvp_kills
# row. The victim's loot sits in the key until it is opened at the Wilderness
# loot chest, where RuneLite's loot tracker reports each key's contents as its
# own "Loot Chest" loot (LootTrackerPlugin.addPvpChestLoot) and the plugin
# forwards it as an ordinary drop. A key carries no victim name, so a key is
# only matched to a kill when that is certain: it was the only key opened, and
# the killer has exactly one bones-only kill since their last opening.
# Otherwise it is kept as a loot_key row, which adds loot to the boards but no
# kill (the kill it came from already counted).
# --------------------------------------------------------------------------- #
KIND_KILL = "kill"
KIND_LOOT_KEY = "loot_key"
UNKNOWN_VICTIM = "Unknown"
LOOT_CHEST_SOURCE = "loot chest"
#: What a kill made with loot keys on leaves on the floor.
KEYED_KILL_ITEMS = frozenset({"bones", "loot key"})
#: A chest opening reports every key at once; keys wait this long so the
#: maintenance sweep sees the whole opening before matching any of it.
LOOT_KEY_GRACE_SECONDS = 45
#: Keys from one player arriving this close together are one opening.
LOOT_KEY_BURST_SECONDS = 30
#: How far back a key may belong (keys can be held; the game caps them at 5).
LOOT_KEY_WINDOW = timedelta(days=7)
LOOT_KEY_DEFERRED_KEY = "pvp:lootkey:deferred"
LOOT_KEY_MAX_ATTEMPTS = 3


def is_loot_chest_embed(processed_data: dict) -> bool:
    """Whether one flattened embed is an item from an opened loot key."""
    if not isinstance(processed_data, dict):
        return False
    declared = str(processed_data.get("type") or "").strip().lower()
    if declared and declared not in _DROP_TYPES:
        return False
    source = processed_data.get("source") or processed_data.get("npc_name")
    return str(source or "").strip().lower() == LOOT_CHEST_SOURCE


# --------------------------------------------------------------------------- #
# Bundle collapse
# --------------------------------------------------------------------------- #
def is_pvp_embed(processed_data: dict) -> bool:
    """Whether one flattened embed is an item from a player kill."""
    if not isinstance(processed_data, dict):
        return False
    declared = str(processed_data.get("type") or "").strip().lower()
    if declared == PVP_KILL_TYPE:
        return False  # already collapsed
    if declared in ("pvp", "player_kill"):
        return True  # an item embed declared as PvP outright
    if declared and declared not in _DROP_TYPES:
        return False
    if is_loot_chest_embed(processed_data):
        return True  # an opened loot key: the deferred half of a kill
    return str(processed_data.get("source_type") or "").strip().lower() == "pvp"


def _int_or_none(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _text(value, limit: int) -> Optional[str]:
    """A trimmed display string; the plugin's literal ``"N/A"`` means absent."""
    if value is None:
        return None
    text = str(value).replace(" ", " ").strip()
    if not text or text.upper() == "N/A":
        return None
    return text[:limit]


def collapse_pvp_bundle(processed_items: List[dict]) -> List[dict]:
    """Fold a payload's PvP item embeds into one ``pvp_kill`` submission.

    Returns the list unchanged when there is no PvP embed. Otherwise the PvP
    embeds are replaced, at the position of the first, by one entry that keeps
    the first embed's envelope fields (player, auth, timestamps, world, plugin
    version) and carries ``pvp_items`` plus ``_bundle_guids``, every item
    GUID, so each one is marked processed for the plugin's status check.

    One payload is one loot event, so it is one kill. Embeds naming different
    victims (never seen, but cheap to respect) become one kill per victim.
    """
    if not processed_items:
        return processed_items
    if not any(is_pvp_embed(item) for item in processed_items):
        return processed_items

    out: List[dict] = []
    by_victim: Dict[str, dict] = {}
    for item in processed_items:
        if not is_pvp_embed(item):
            out.append(item)
            continue
        loot_key = is_loot_chest_embed(item)
        if loot_key:
            # One payload is one key; the name in "source" is the chest's.
            victim, bucket = UNKNOWN_VICTIM, "\x00loot_key"
        else:
            victim = _text(item.get("source") or item.get("npc_name"), 64) or "Unknown"
            bucket = victim.lower()
        kill = by_victim.get(bucket)
        if kill is None:
            kill = {
                k: v for k, v in item.items()
                if k not in ("item", "item_name", "id", "item_id", "quantity", "value",
                             "killcount", "kill_count", "players_included",
                             "nearby_players", "members")
            }
            kill["type"] = PVP_KILL_TYPE
            kill["victim_name"] = victim
            kill["pvp_items"] = []
            kill["_bundle_guids"] = []
            if loot_key:
                kill["loot_key"] = True
            by_victim[bucket] = kill
            out.append(kill)
        kill["pvp_items"].append({
            "item_id": item.get("item_id", item.get("id")),
            "item_name": item.get("item_name", item.get("item")),
            "quantity": item.get("quantity"),
            "value": item.get("value"),
        })
        guid = item.get("guid") or item.get("unique_id")
        if guid:
            kill["_bundle_guids"].append(guid)
        # Fields only some embeds carry (a screenshot path) fill gaps.
        for key, value in item.items():
            if key not in kill and key not in ("item", "item_name", "id", "item_id",
                                               "quantity", "value"):
                kill[key] = value
    for kill in by_victim.values():
        if kill["_bundle_guids"]:
            kill["guid"] = kill["_bundle_guids"][0]
    return out


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def global_exclusion(region_id, killer, victim) -> Optional[str]:
    """Why a kill counts nowhere, or None.

    ``killer``/``victim`` are Player rows (victim may be None: untracked).
    A user id of 0 is a real account, so ownership compares with ``is not
    None``, never truthiness.
    """
    from utils.death_regions import pvp_minigame_for_region

    minigame = pvp_minigame_for_region(region_id)
    if minigame:
        return f"pvp_minigame:{minigame}"
    if victim is not None:
        if victim.player_id == killer.player_id:
            return "own_account"
        killer_user = getattr(killer, "user_id", None)
        victim_user = getattr(victim, "user_id", None)
        if killer_user is not None and victim_user is not None and killer_user == victim_user:
            return "own_account"
    return None


def same_victim_limit(config: Dict[Any, str], group_id: int) -> Optional[int]:
    """The group's daily cap on counted kills of one victim; None = no cap."""
    from utils import group_config as gc

    if gc.is_truthy(config.get((group_id, SAME_VICTIM_UNLIMITED_KEY))):
        return None
    raw = config.get((group_id, SAME_VICTIM_LIMIT_KEY))
    limit = _int_or_none(raw)
    # No row, an unparseable value or the legacy "0" unset sentinel all mean
    # the default: "no limit" is its own switch, so 0 never has to mean it.
    if limit is None or limit <= 0:
        return DEFAULT_SAME_VICTIM_LIMIT
    return limit


def _min_value(config: Dict[Any, str], group_id: int, key: str, default: int) -> int:
    value = _int_or_none(config.get((group_id, key)))
    return default if value is None or value < 0 else value


def _counted_today(session, group_id: int, killer_id: int, victim_name: str,
                   when: datetime, exclude_kill_id=None) -> int:
    from db.models import PvpKill, PvpKillGroup

    day_start = datetime(when.year, when.month, when.day)
    query = (
        session.query(func.count(PvpKill.id))
        .join(PvpKillGroup, PvpKillGroup.kill_id == PvpKill.id)
        .filter(
            PvpKill.player_id == killer_id,
            PvpKill.date_added >= day_start,
            PvpKill.date_added < day_start + timedelta(days=1),
            PvpKill.victim_name == victim_name,
            PvpKill.kind == KIND_KILL,
            PvpKill.hidden.is_(False),
            PvpKillGroup.group_id == group_id,
            PvpKillGroup.counted.is_(True),
        )
    )
    if exclude_kill_id is not None:
        query = query.filter(PvpKill.id != exclude_kill_id)
    return int(query.scalar() or 0)


def _victim_groups(session, victim_player_id: Optional[int]) -> set:
    if victim_player_id is None:
        return set()
    from sqlalchemy import text

    rows = session.execute(
        text("SELECT group_id FROM user_group_association WHERE player_id = :pid"),
        {"pid": int(victim_player_id)},
    ).all()
    return {int(r[0]) for r in rows}


def group_verdict(session, kill, group_id: int, config: Dict[Any, str],
                  victim_group_ids: set) -> Optional[str]:
    """Why this kill does not count for this group, or None when it does."""
    from utils import group_config as gc

    if (group_id != GLOBAL_GROUP_ID and group_id in victim_group_ids
            and not gc.is_truthy(config.get((group_id, COUNT_MEMBER_KILLS_KEY)))):
        return "member_kill"
    limit = same_victim_limit(config, group_id)
    if limit is not None:
        already = _counted_today(session, group_id, kill.player_id, kill.victim_name,
                                 kill.date_added, exclude_kill_id=kill.id)
        if already >= limit:
            return "same_victim_limit"
    return None


def _resolve_victim(session, victim_name: str):
    """The tracked Player behind a victim's RSN, or None. Indexed lookups only
    (exact name, then the game's '-'/'_'/' ' equivalence via player_name_norm),
    through the same helper the claim and points commands use."""
    from db.models import Player
    from utils.rsn import find_player_by_rsn

    if not victim_name or victim_name == "Unknown":
        return None
    try:
        return find_player_by_rsn(session, Player, victim_name)
    except Exception:
        return None


def _region_name(region_id) -> Optional[str]:
    if region_id is None:
        return None
    try:
        from utils import region_names

        return _text(region_names.location_for(region_id) or region_names.name_for(region_id), 125)
    except Exception:
        return None


def _items_text(items: List[dict], limit: int = NOTIFY_ITEM_LINES) -> str:
    """``2 x Abyssal whip (2.4M)`` lines, most valuable first."""
    from utils.format import format_number

    ordered = sorted(items, key=lambda i: i["total"], reverse=True)
    lines = []
    for item in ordered[:limit]:
        qty = f"{item['quantity']:,} x " if item["quantity"] > 1 else ""
        lines.append(f"{qty}{item['item_name']} ({format_number(item['total'])})")
    rest = ordered[limit:]
    if rest:
        rest_value = sum(i["total"] for i in rest)
        lines.append(f"+{len(rest)} more ({format_number(rest_value)})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Processor
# --------------------------------------------------------------------------- #
async def _resolve_items(session, raw_items, player_name: str) -> List[dict]:
    """A kill's items as recorded: known items only, valued like drops."""
    items: List[dict] = []
    for raw in raw_items or []:
        await asyncio.sleep(0)
        quantity = sanitize_quantity(raw.get("quantity"))
        if quantity is None:
            continue
        item = await ensure_item_for_drop(session, raw.get("item_id"), raw.get("item_name"))
        if not item:
            debug_print(f"[PvP] Unknown item {raw.get('item_name')!r}; left off the kill")
            continue
        unit = int(await get_true_item_value(
            item.item_name, _int_or_none(raw.get("value")) or 0, item_id=item.item_id
        ) or 0)
        if exceeds_value_sanity_limit(unit, quantity):
            print(f"[PvP] Rejected implausible item total {item.item_name} x{quantity} "
                  f"from {player_name}")
            continue
        items.append({
            "item_id": int(item.item_id),
            "item_name": item.item_name,
            "quantity": int(quantity),
            "value": unit,
            "total": unit * int(quantity),
        })
    return items


def _pvp_config(session, group_ids) -> Dict[Any, str]:
    from utils import group_config as gc

    return gc.get_bulk(session, list(group_ids), [
        MIN_VALUE_KEY, COUNT_MEMBER_KILLS_KEY, SAME_VICTIM_LIMIT_KEY,
        SAME_VICTIM_UNLIMITED_KEY,
    ])


def _mark_boards_dirty(group_ids) -> None:
    try:
        from lootboard import schedule as lootboard_schedule

        for group_id in group_ids:
            lootboard_schedule.mark_dirty(group_id, kind="pvp")
    except Exception:
        pass


async def _announce(session, kill, *, player, player_name: str, items: List[dict], groups,
                    config: Dict[Any, str], image_url, kill_data: dict,
                    existing_session) -> str:
    """Queue the kill's message for every group in ``groups`` whose minimum it
    meets. Returns the notice for the plugin ("" when nothing was sent)."""
    from services import pvp_standings

    loot_key = kill.kind == KIND_LOOT_KEY or kill.loot_key_at is not None
    victim_known = kill.kind != KIND_LOOT_KEY
    total_value = int(kill.total_value or 0)
    items_text = _items_text(items)
    top_item = max(items, key=lambda i: i["total"])
    notice = ""
    sent_to = []
    for group in groups:
        await asyncio.sleep(0)
        group_id = group.group_id
        if total_value < _min_value(config, group_id, MIN_VALUE_KEY, DEFAULT_MIN_VALUE):
            continue
        if await screenshot_required(session, group_id) and not image_url:
            what = f"PvP kill on {kill.victim_name}" if victim_known else "Loot key"
            notice = screenshot_gate_notice(
                session, player.player_id, group.group_name, what, value=total_value,
            )
            continue
        line = {}
        try:
            line = pvp_standings.player_line(player.player_id, month_token(kill.date_added),
                                             group_id)
        except Exception:
            pass
        notification_data = {
            "kill_id": kill.id,
            "guid": kill_data.get("guid"),
            "player_name": player_name,
            "player_id": player.player_id,
            # A key nobody could be matched to names no one; the message says
            # it came from a loot key instead.
            "victim_name": kill.victim_name if victim_known else "",
            "victim_player_id": kill.victim_player_id,
            "victim_combat_level": kill.victim_combat_level,
            "total_value": total_value,
            "item_count": kill.item_count,
            "items_text": items_text,
            "top_item_name": top_item["item_name"],
            "top_item_id": top_item["item_id"],
            "world": kill.world,
            "wilderness_level": kill.wilderness_level,
            "location": kill.location,
            "region_id": kill.region_id,
            "image_url": image_url or "",
            "video_key": kill_data.get("video_key"),
            "kills_this_month": line.get("kills"),
            "loot_this_month": line.get("loot"),
            "pvp_rank": line.get("rank"),
            "world_type": kill.world_type,
            "plugin_version": kill_data.get("p_v"),
            "loot_key": loot_key,
        }
        queued = await create_notification(
            PVP_KILL_TYPE, player.player_id, notification_data, group_id,
            existing_session=existing_session,
        )
        if queued and group_id > GLOBAL_GROUP_ID:
            sent_to.append(group.group_name)
    if sent_to and not notice:
        what = "Loot key recorded" if loot_key else "PvP kill recorded"
        notice = f"{what} - a message has been sent to {', '.join(sent_to)}"
    return notice


async def pvp_kill_processor(kill_data, external_session=None):
    """Record one PvP kill and announce it where it counts."""
    from db.models import PvpKill, PvpKillGroup, PvpKillItem
    from services import pvp_standings

    if kill_data.get("loot_key"):
        return await defer_loot_key(kill_data)

    session, use_external_session = select_session_and_flag(external_session)

    player_name = str(kill_data.get("player_name") or kill_data.get("player") or "").strip()
    account_hash = kill_data.get("acc_hash")
    auth_key = kill_data.get("auth_key")
    guid = kill_data.get("guid")
    victim_name = _text(kill_data.get("victim_name") or kill_data.get("source"), 64) or "Unknown"
    plugin_version = kill_data.get("p_v")
    # Never the client's raw image_url: the shared resolver only trusts a URL
    # the server wrote after downloading the screenshot (security audit
    # 2026-08-02: a client-authored path once read arbitrary server files).
    image_url, _attachment_type = resolve_attachment_from_drop_data(kill_data)
    image_url = image_url or None
    raw_items = kill_data.get("pvp_items") or []

    if not player_name or not account_hash:
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    if not raw_items:
        return SubmissionResponse(success=False, message="PvP kill carried no items.")

    try:
        if guid:
            existing = session.query(PvpKill.id).filter(PvpKill.unique_id == str(guid)).first()
            if existing:
                return SubmissionResponse(success=True, message="PvP kill already recorded.")

        player, authed, user_exists = await ensure_player_and_auth(
            session, player_name, str(account_hash), auth_key
        )
        if not player:
            return SubmissionResponse(success=False, message=f"Player {player_name} not found in the database")
        if not user_exists or not authed:
            return SubmissionResponse(success=False, message=f"Player {player_name} failed auth check")

        items = await _resolve_items(session, raw_items, player_name)
        if not items:
            return SubmissionResponse(success=False, message="No PvP loot items could be recorded.")

        region_id = _int_or_none(kill_data.get("region_id"))
        victim = _resolve_victim(session, victim_name)
        when = received_at(kill_data)
        total_value = sum(i["total"] for i in items)
        world_type = str(kill_data.get("world_type") or "main").lower()

        kill = PvpKill(
            player_id=player.player_id,
            victim_name=victim_name,
            victim_player_id=victim.player_id if victim is not None else None,
            victim_combat_level=_int_or_none(kill_data.get("victim_combat_level")),
            world=_int_or_none(kill_data.get("world")),
            wilderness_level=_int_or_none(kill_data.get("wilderness_level")),
            region_id=region_id,
            # The plugin sends the region, not a name; the server's names also
            # cover the Wilderness (utils/region_names.py), like deaths.
            location=_text(kill_data.get("location"), 125) or _region_name(region_id),
            total_value=total_value,
            item_count=sum(i["quantity"] for i in items),
            world_type=world_type,
            image_url=image_url,
            unique_id=str(guid) if guid else None,
            used_api=bool(kill_data.get("used_api", False)),
            date_added=when,
            partition=when.year * 100 + when.month,
            excluded_reason=global_exclusion(region_id, player, victim),
            plugin_version=str(plugin_version)[:20] if plugin_version else None,
            kind=KIND_KILL,
        )
        kill.items = [
            PvpKillItem(item_id=i["item_id"], item_name=i["item_name"],
                        quantity=i["quantity"], value=i["value"])
            for i in items
        ]
        session.add(kill)
        session.flush()

        if kill.excluded_reason:
            debug_print(f"[PvP] Kill {kill.id} stored, counts nowhere: {kill.excluded_reason}")
            if not use_external_session:
                session.commit()
            return SubmissionResponse(success=True, message="PvP kill recorded.")

        player_groups = get_player_groups_with_global(session, player)
        config = _pvp_config(session, [g.group_id for g in player_groups])
        victim_group_ids = _victim_groups(session, kill.victim_player_id)

        counted_groups = []
        for group in player_groups:
            reason = group_verdict(session, kill, group.group_id, config, victim_group_ids)
            session.add(PvpKillGroup(kill_id=kill.id, group_id=group.group_id,
                                     counted=reason is None, reason=reason))
            if reason is None:
                counted_groups.append(group)
        session.flush()

        pvp_standings.record_kill(kill, [g.group_id for g in counted_groups],
                                  player_name=player_name)
        _mark_boards_dirty([g.group_id for g in counted_groups])

        notice = await _announce(
            session, kill, player=player, player_name=player_name, items=items,
            groups=counted_groups, config=config, image_url=image_url, kill_data=kill_data,
            existing_session=session if use_external_session else None,
        )

        if not use_external_session:
            session.commit()
        return SubmissionResponse(success=True, message="PvP kill recorded.",
                                  notice=notice or None)
    except Exception:
        if not use_external_session:
            session.rollback()
        raise


# --------------------------------------------------------------------------- #
# Loot keys: deferral, matching, recording
# --------------------------------------------------------------------------- #
def _redis():
    from utils.redis import redis_client

    return redis_client.client


def _player_key(kill_data: dict) -> tuple:
    return (str(kill_data.get("player_name") or kill_data.get("player") or "").strip().lower(),
            str(kill_data.get("acc_hash") or ""))


async def defer_loot_key(kill_data: dict) -> SubmissionResponse:
    """Park one opened key until the rest of its chest opening has arrived.

    Matching a key to a kill is only safe when it was the only key opened, and
    the plugin sends each key separately, so a key cannot be judged alone. The
    webhook consumer's maintenance tick replays parked keys after
    ``LOOT_KEY_GRACE_SECONDS`` (``process_due_loot_keys``)."""
    import json
    import time

    if not _player_key(kill_data)[0] or not kill_data.get("acc_hash"):
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    if not kill_data.get("pvp_items"):
        return SubmissionResponse(success=False, message="Loot key carried no items.")
    now = time.time()
    entry = {"kill_data": kill_data, "arrived": now, "attempts": 0}
    try:
        _redis().zadd(LOOT_KEY_DEFERRED_KEY,
                      {json.dumps(entry, default=str): now + LOOT_KEY_GRACE_SECONDS})
    except Exception as e:
        # Redis down: record it now, unmatched. Without the rest of the
        # opening there is no telling which kill it belongs to.
        print(f"[PvP] Couldn't park a loot key ({e}); recording it unmatched")
        await process_loot_key_burst([kill_data], matchable=False)
    return SubmissionResponse(success=True, message="Loot key received.")


async def process_due_loot_keys(limit: int = 200) -> int:
    """Replay parked keys whose grace has passed, one chest opening at a time.

    Called by the webhook consumer's maintenance tick (~1/min). A player's
    keys that arrived within ``LOOT_KEY_BURST_SECONDS`` of the first due one
    form one opening. Each entry is claimed with ZREM, so two sweepers never
    run the same key; a failed opening is retried a few times, then dropped.
    Returns the number of keys replayed."""
    import json
    import time

    client = _redis()
    try:
        due = client.zrangebyscore(LOOT_KEY_DEFERRED_KEY, "-inf", time.time(),
                                   start=0, num=int(limit))
        if not due:
            return 0
        pending = client.zrange(LOOT_KEY_DEFERRED_KEY, 0, -1)
    except Exception:
        return 0

    entries = {}
    for raw in pending:
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            entries[raw] = json.loads(text)
        except Exception:
            client.zrem(LOOT_KEY_DEFERRED_KEY, raw)
    players = []
    for raw in due:
        entry = entries.get(raw)
        if entry is not None and _player_key(entry["kill_data"]) not in players:
            players.append(_player_key(entry["kill_data"]))

    replayed = 0
    for who in players:
        mine = sorted(
            ((raw, e) for raw, e in entries.items() if _player_key(e["kill_data"]) == who),
            key=lambda pair: pair[1].get("arrived") or 0,
        )
        if not mine:
            continue
        first = mine[0][1].get("arrived") or 0
        opening = []
        for raw, entry in mine:
            if (entry.get("arrived") or 0) - first > LOOT_KEY_BURST_SECONDS:
                break
            try:
                if client.zrem(LOOT_KEY_DEFERRED_KEY, raw):
                    opening.append(entry)
            except Exception:
                continue
        if not opening:
            continue
        try:
            await process_loot_key_burst([e["kill_data"] for e in opening])
            replayed += len(opening)
        except Exception as e:
            print(f"[PvP] Loot key replay failed for {who[0]!r}: {e}")
            for entry in opening:
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                if entry["attempts"] < LOOT_KEY_MAX_ATTEMPTS:
                    try:
                        client.zadd(LOOT_KEY_DEFERRED_KEY,
                                    {json.dumps(entry, default=str): time.time() + 60})
                    except Exception:
                        pass
    return replayed


def _loot_key_seen(session, guid: str) -> bool:
    from db.models import PvpKill
    from sqlalchemy import or_

    return session.query(PvpKill.id).filter(
        or_(PvpKill.unique_id == guid, PvpKill.loot_key_guid == guid)
    ).first() is not None


def last_key_opening(session, player_id: int, before: datetime) -> Optional[datetime]:
    """When this player last opened a key we recorded (matched or not)."""
    from db.models import PvpKill

    unmatched = session.query(func.max(PvpKill.date_added)).filter(
        PvpKill.player_id == player_id, PvpKill.kind == KIND_LOOT_KEY,
        PvpKill.date_added < before,
    ).scalar()
    matched = session.query(func.max(PvpKill.loot_key_at)).filter(
        PvpKill.player_id == player_id, PvpKill.loot_key_at < before,
    ).scalar()
    found = [t for t in (unmatched, matched) if t is not None]
    return max(found) if found else None


def is_keyed_kill_shape(items) -> bool:
    """A kill that left only bones (and maybe the key) on the floor."""
    names = [str(getattr(i, "item_name", None) or "").strip().lower() for i in items or ()]
    return bool(names) and all(name in KEYED_KILL_ITEMS for name in names)


def loot_key_candidates(session, player_id: int, opened: datetime) -> list:
    """The kills an opened key could have come from: this killer's bones-only
    kills since their last recorded opening (at most ``LOOT_KEY_WINDOW`` back)
    that have no key yet."""
    from db.models import PvpKill

    since = opened - LOOT_KEY_WINDOW
    last = last_key_opening(session, player_id, opened)
    if last is not None and last > since:
        since = last
    kills = (
        session.query(PvpKill)
        .filter(
            PvpKill.player_id == player_id,
            PvpKill.kind == KIND_KILL,
            PvpKill.loot_key_at.is_(None),
            PvpKill.date_added > since,
            PvpKill.date_added <= opened,
        )
        .order_by(PvpKill.id)
        .all()
    )
    return [k for k in kills if is_keyed_kill_shape(k.items)]


async def process_loot_key_burst(keys: List[dict], *, matchable: bool = True) -> int:
    """Record one chest opening's keys (all from one player).

    One key and exactly one candidate kill: the key's loot joins that kill,
    which keeps its victim, its group verdicts and its date. Anything else is
    ambiguous, so each key becomes its own ``loot_key`` row instead."""
    if not keys:
        return 0
    first = keys[0]
    player_name = str(first.get("player_name") or first.get("player") or "").strip()
    session, _external = select_session_and_flag(None)
    try:
        player, authed, user_exists = await ensure_player_and_auth(
            session, player_name, str(first.get("acc_hash")), first.get("auth_key")
        )
        if not player or not user_exists or not authed:
            session.rollback()
            print(f"[PvP] Loot key from {player_name!r} failed the player/auth check")
            return 0

        resolved = []
        for kill_data in keys:
            guid = kill_data.get("guid")
            if guid and _loot_key_seen(session, str(guid)):
                continue
            items = await _resolve_items(session, kill_data.get("pvp_items"), player_name)
            if items:
                resolved.append((kill_data, items))
        if not resolved:
            session.commit()
            return 0

        opened = min(received_at(kill_data) for kill_data, _ in resolved)
        target = None
        if matchable and len(keys) == 1:
            candidates = loot_key_candidates(session, player.player_id, opened)
            if len(candidates) == 1:
                target = candidates[0]

        if target is not None:
            kill_data, items = resolved[0]
            await _merge_loot_key(session, target, kill_data, items, player, player_name, opened)
        else:
            for kill_data, items in resolved:
                await _record_loot_key(session, kill_data, items, player, player_name)
        session.commit()
        return len(resolved)
    except Exception:
        session.rollback()
        raise


async def _merge_loot_key(session, kill, kill_data, items, player, player_name, opened):
    """Add a key's loot to the kill it came from."""
    from db.models import PvpKillGroup, PvpKillItem
    from services import pvp_standings

    added = sum(i["total"] for i in items)
    for i in items:
        kill.items.append(PvpKillItem(item_id=i["item_id"], item_name=i["item_name"],
                                      quantity=i["quantity"], value=i["value"]))
    kill.total_value = int(kill.total_value or 0) + added
    kill.item_count = int(kill.item_count or 0) + sum(i["quantity"] for i in items)
    guid = kill_data.get("guid")
    kill.loot_key_guid = str(guid)[:255] if guid else None
    kill.loot_key_at = opened
    image_url, _attachment_type = resolve_attachment_from_drop_data(kill_data)
    if image_url and not kill.image_url:
        kill.image_url = image_url
    session.flush()
    debug_print(f"[PvP] Loot key ({added:,} gp) matched to kill {kill.id}")

    if kill.hidden or kill.excluded_reason:
        return
    counted_ids = {
        int(gid) for (gid,) in session.query(PvpKillGroup.group_id).filter(
            PvpKillGroup.kill_id == kill.id, PvpKillGroup.counted.is_(True))
    }
    if not counted_ids:
        return
    pvp_standings.add_loot(kill, added, counted_ids)
    _mark_boards_dirty(counted_ids)

    groups = [g for g in get_player_groups_with_global(session, player)
              if g.group_id in counted_ids]
    all_items = [{
        "item_id": int(i.item_id), "item_name": i.item_name, "quantity": int(i.quantity or 0),
        "value": int(i.value or 0), "total": int(i.value or 0) * int(i.quantity or 0),
    } for i in kill.items]
    await _announce(
        session, kill, player=player, player_name=player_name, items=all_items, groups=groups,
        config=_pvp_config(session, [g.group_id for g in groups]),
        image_url=kill.image_url, kill_data=kill_data, existing_session=session,
    )


async def _record_loot_key(session, kill_data, items, player, player_name):
    """A key that could not be matched to one kill: its loot counts, as a
    ``loot_key`` row, in every group the killer is in. No kill is added (the
    kill it came from is already on the boards), and with no victim the
    clanmate and same-victim rules have nothing to apply to."""
    from db.models import PvpKill, PvpKillGroup, PvpKillItem
    from services import pvp_standings

    when = received_at(kill_data)
    guid = kill_data.get("guid")
    plugin_version = kill_data.get("p_v")
    image_url, _attachment_type = resolve_attachment_from_drop_data(kill_data)
    kill = PvpKill(
        player_id=player.player_id,
        victim_name=UNKNOWN_VICTIM,
        world=_int_or_none(kill_data.get("world")),
        region_id=_int_or_none(kill_data.get("region_id")),
        total_value=sum(i["total"] for i in items),
        item_count=sum(i["quantity"] for i in items),
        world_type=str(kill_data.get("world_type") or "main").lower(),
        image_url=image_url or None,
        unique_id=str(guid) if guid else None,
        used_api=bool(kill_data.get("used_api", False)),
        date_added=when,
        partition=when.year * 100 + when.month,
        plugin_version=str(plugin_version)[:20] if plugin_version else None,
        kind=KIND_LOOT_KEY,
    )
    kill.items = [
        PvpKillItem(item_id=i["item_id"], item_name=i["item_name"],
                    quantity=i["quantity"], value=i["value"])
        for i in items
    ]
    session.add(kill)
    session.flush()

    groups = get_player_groups_with_global(session, player)
    for group in groups:
        session.add(PvpKillGroup(kill_id=kill.id, group_id=group.group_id,
                                 counted=True, reason=None))
    session.flush()
    group_ids = [g.group_id for g in groups]
    pvp_standings.record_kill(kill, group_ids, player_name=player_name)
    _mark_boards_dirty(group_ids)
    await _announce(
        session, kill, player=player, player_name=player_name, items=items, groups=groups,
        config=_pvp_config(session, group_ids), image_url=kill.image_url,
        kill_data=kill_data, existing_session=session,
    )

# --------------------------------------------------------------------------- #
# Deaths
# --------------------------------------------------------------------------- #
def is_pvp_death(killer_type, region_id, world_type) -> bool:
    """A death to another player that the PvP standings count."""
    from utils.death_regions import pvp_minigame_for_region

    if str(killer_type or "").lower() != "player":
        return False
    if str(world_type or "main").lower() != "main":
        return False
    return pvp_minigame_for_region(region_id) is None


async def record_pvp_death(session, player, death_entry, player_groups, death_data: dict,
                           *, use_external_session: bool) -> None:
    """Count a death to another player and post it for groups that asked.

    Called by the death processor for every main-world death whose killer was
    a player. Best-effort: a failure here never costs the death its row.
    """
    from services import pvp_standings
    from utils import group_config as gc

    group_ids = [g.group_id for g in player_groups]
    pvp_standings.record_death(player.player_id, group_ids,
                               getattr(death_entry, "date_added", None) or datetime.now())

    if not group_ids:
        return
    config = gc.get_bulk(session, group_ids, [NOTIFY_DEATHS_KEY, DEATH_MIN_VALUE_KEY])
    value_lost = death_data.get("value_lost")
    for group in player_groups:
        await asyncio.sleep(0)
        group_id = group.group_id
        if not gc.is_truthy(config.get((group_id, NOTIFY_DEATHS_KEY))):
            continue
        minimum = _min_value(config, group_id, DEATH_MIN_VALUE_KEY, DEFAULT_DEATH_MIN_VALUE)
        if minimum and (value_lost is None or int(value_lost) < minimum):
            continue
        await create_notification(
            "pvp_death",
            player.player_id,
            {
                "death_id": death_entry.id,
                "guid": death_data.get("guid"),
                "player_name": death_data.get("player_name"),
                "player_id": player.player_id,
                "killer_name": death_data.get("source") or "",
                "killer_combat_level": death_data.get("killer_combat_level"),
                "location": death_data.get("location") or "",
                "region_id": death_data.get("region_id"),
                "value_lost": value_lost,
                "image_url": death_data.get("image_url") or "",
                "video_key": death_data.get("video_key"),
                "world_type": "main",
                "plugin_version": death_data.get("plugin_version"),
            },
            group_id,
            existing_session=session if use_external_session else None,
        )
