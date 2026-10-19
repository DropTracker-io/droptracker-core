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
        victim = _text(item.get("source") or item.get("npc_name"), 64) or "Unknown"
        kill = by_victim.get(victim.lower())
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
            by_victim[victim.lower()] = kill
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
async def pvp_kill_processor(kill_data, external_session=None):
    """Record one PvP kill and announce it where it counts."""
    from db.models import PvpKill, PvpKillGroup, PvpKillItem
    from services import pvp_standings

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

        items: List[dict] = []
        for raw in raw_items:
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
            location=_text(kill_data.get("location"), 125),
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

        from utils import group_config as gc

        player_groups = get_player_groups_with_global(session, player)
        group_ids = [g.group_id for g in player_groups]
        config = gc.get_bulk(session, group_ids, [
            MIN_VALUE_KEY, COUNT_MEMBER_KILLS_KEY, SAME_VICTIM_LIMIT_KEY,
            SAME_VICTIM_UNLIMITED_KEY,
        ])
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

        try:
            from lootboard import schedule as lootboard_schedule

            for group in counted_groups:
                lootboard_schedule.mark_dirty(group.group_id, kind="pvp")
        except Exception:
            pass

        notice = ""
        items_text = _items_text(items)
        top_item = max(items, key=lambda i: i["total"])
        sent_to = []
        for group in counted_groups:
            await asyncio.sleep(0)
            group_id = group.group_id
            if total_value < _min_value(config, group_id, MIN_VALUE_KEY, DEFAULT_MIN_VALUE):
                continue
            if await screenshot_required(session, group_id) and not image_url:
                notice = screenshot_gate_notice(
                    session, player.player_id, group.group_name, f"PvP kill on {victim_name}",
                    value=total_value,
                )
                continue
            token = month_token(when)
            line = {}
            try:
                line = pvp_standings.player_line(player.player_id, token, group_id)
            except Exception:
                pass
            notification_data = {
                "kill_id": kill.id,
                "guid": guid,
                "player_name": player_name,
                "player_id": player.player_id,
                "victim_name": victim_name,
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
                "region_id": region_id,
                "image_url": image_url or "",
                "video_key": kill_data.get("video_key"),
                "kills_this_month": line.get("kills"),
                "loot_this_month": line.get("loot"),
                "pvp_rank": line.get("rank"),
                "world_type": world_type,
                "plugin_version": plugin_version,
            }
            queued = await create_notification(
                PVP_KILL_TYPE, player.player_id, notification_data, group_id,
                existing_session=session if use_external_session else None,
            )
            if queued and group_id > GLOBAL_GROUP_ID:
                sent_to.append(group.group_name)

        if not use_external_session:
            session.commit()
        if sent_to and not notice:
            notice = f"PvP kill recorded - a message has been sent to {', '.join(sent_to)}"
        return SubmissionResponse(success=True, message="PvP kill recorded.",
                                  notice=notice or None)
    except Exception:
        if not use_external_session:
            session.rollback()
        raise


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
