"""The PvP lootboard: one month of loot taken from player kills.

Same canvas as the drop board (``lootboard/generator.py``): the item grid, the
top-12 player list and the recent strip are drawn by the same code from the
same background styles, so a group's two boards look like a pair. What
differs is the data. It comes only from ``pvp_kills`` rows that counted for
this group (``pvp_kill_groups.counted``), and the recent strip shows kills
(the killer and the best item they took) rather than single drops.

Output: ``static/assets/img/clans/{group_id}/lb/pvp_lootboard.png``. The
board generator service redraws it on the group's lootboard schedule
(``lootboard/schedule.py``, kind ``pvp``) and the core bot posts it to the
group's ``pvp_lootboard_channel_id``.
"""
from __future__ import annotations

import calendar
import csv
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageFont

GLOBAL_GROUP_ID = 2
CLANS_DIR = "/store/droptracker/disc/static/assets/img/clans"
RECENT_SLOTS = 12


def board_path(group_id: int) -> str:
    return f"{CLANS_DIR}/{int(group_id)}/lb/pvp_lootboard.png"


def _partition_now() -> int:
    now = datetime.now()
    return now.year * 100 + now.month


def collect_board_data(session, group_id: int, partition: Optional[int] = None
                       ) -> Tuple[Dict[str, str], Dict[int, int], List[dict], int]:
    """``(group_items, player_totals, recent_kills, total_loot)`` for one month.

    ``group_items`` uses the drop board's ``{"item_id": "qty,total"}`` shape so
    ``draw_drops_on_image`` renders it unchanged. Kills of players who have
    since left the group still count: the kill was theirs while a member,
    which is what ``pvp_kill_groups`` recorded at intake.
    """
    from db.models import PvpKill, PvpKillGroup, PvpKillItem

    partition = int(partition or _partition_now())
    kills_q = (
        session.query(PvpKill.id, PvpKill.player_id, PvpKill.total_value, PvpKill.date_added,
                      PvpKill.victim_name)
        .join(PvpKillGroup, PvpKillGroup.kill_id == PvpKill.id)
        .filter(
            PvpKill.partition == partition,
            PvpKill.hidden.is_(False),
            PvpKill.excluded_reason.is_(None),
            PvpKillGroup.group_id == int(group_id),
            PvpKillGroup.counted.is_(True),
        )
    )
    kills = kills_q.all()
    if not kills:
        return {}, {}, [], 0

    kill_ids = [k.id for k in kills]
    items_by_kill: Dict[int, List[tuple]] = defaultdict(list)
    for start in range(0, len(kill_ids), 2000):
        chunk = kill_ids[start:start + 2000]
        for kill_id, item_id, quantity, value in (
            session.query(PvpKillItem.kill_id, PvpKillItem.item_id, PvpKillItem.quantity,
                          PvpKillItem.value)
            .filter(PvpKillItem.kill_id.in_(chunk))
            .all()
        ):
            items_by_kill[int(kill_id)].append((int(item_id), int(quantity or 0), int(value or 0)))

    grid: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    player_totals: Dict[int, int] = defaultdict(int)
    recent: List[dict] = []
    for kill in kills:
        items = items_by_kill.get(int(kill.id), [])
        for item_id, quantity, value in items:
            grid[str(item_id)][0] += quantity
            grid[str(item_id)][1] += quantity * value
        player_totals[int(kill.player_id)] += int(kill.total_value or 0)
        if items:
            best = max(items, key=lambda i: i[1] * i[2])
            recent.append({
                "kill_id": int(kill.id),
                "player_id": int(kill.player_id),
                "victim_name": kill.victim_name,
                "item_id": best[0],
                "quantity": best[1],
                "value": int(kill.total_value or 0),
                "date_added": kill.date_added,
            })
    recent.sort(key=lambda r: r["kill_id"], reverse=True)
    group_items = {item_id: f"{q},{t}" for item_id, (q, t) in grid.items()}
    total = sum(player_totals.values())
    return group_items, dict(player_totals), recent, total


async def _draw_header(bg_img, draw, title: str, total_loot: int, *, dynamic_colors: bool):
    from lootboard.generator import black, get_dynamic_color, get_value_color, main_font, yellow
    from utils.format import format_number

    value_text = format_number(total_loot)
    prefix = f"{title} - "
    prefix_w = draw.textbbox((0, 0), prefix, font=main_font)[2]
    value_w = draw.textbbox((0, 0), value_text, font=main_font)[2]
    x = int((bg_img.size[0] - (prefix_w + value_w)) / 2)
    y = 20
    text_color = get_dynamic_color(bg_img) if dynamic_colors else yellow
    draw.text((x, y), prefix, font=main_font, fill=text_color, stroke_width=2, stroke_fill=black)
    draw.text((x + prefix_w, y), value_text, font=main_font, fill=get_value_color(total_loot),
              stroke_width=1, stroke_fill=black)
    return bg_img


async def _draw_recent_kills(bg_img, draw, recent: List[dict], names: Dict[int, str], min_value: int,
                             *, dynamic_colors: bool):
    """The recent strip: each kill's best item, its killer and how long ago."""
    from lootboard.generator import (
        black, center_image, get_coin_image_id, get_dynamic_color, load_image_from_id,
        rs_font_path, yellow,
    )

    small_font = ImageFont.truetype(rs_font_path, 18)
    with open("data/recent-mapping.csv", "r") as csvfile:
        slots = {i: row for i, row in enumerate(csv.DictReader(csvfile))}

    shown = [r for r in recent if r["value"] >= min_value][:RECENT_SLOTS]
    text_color = get_dynamic_color(bg_img) if dynamic_colors else yellow
    for i, kill in enumerate(shown):
        if i not in slots:
            break
        item_id = kill["item_id"]
        if int(item_id) == 995:
            item_img = await load_image_from_id(get_coin_image_id(max(int(kill["quantity"]), 1)))
        else:
            item_img = await load_image_from_id(item_id)
        if not item_img:
            continue
        x, y = int(slots[i]["x"]), int(slots[i]["y"])
        resized = item_img.resize((round(item_img.width * 1.3), round(item_img.height * 1.3)),
                                  Image.Resampling.LANCZOS)
        fixed = center_image(resized, 75, 60)
        bg_img.paste(fixed, (x - 5, y - 12), fixed)

        since = datetime.now() - kill["date_added"]
        days, hours, minutes = since.days, since.seconds // 3600, (since.seconds // 60) % 60
        if days > 0:
            ago = f"({days}d {hours}h)"
        elif hours > 0:
            ago = f"({hours}h {minutes}m)"
        else:
            ago = f"({minutes}m)"
        name = names.get(int(kill["player_id"]), "Unknown")
        draw.text((x + 6, y - 10), name, font=small_font, fill=text_color,
                  stroke_width=1, stroke_fill=black)
        draw.text((x, y + 35), ago, font=small_font, fill=text_color,
                  stroke_width=1, stroke_fill=black)
    return bg_img


async def generate_pvp_board(group_id: int, session, partition: Optional[int] = None) -> Optional[str]:
    """Draw and save one group's PvP board; returns the file path."""
    from db.models import Group, GroupConfiguration, LootboardStyle, Player
    from lootboard.generator import (
        _ensure_public_dir, _save_public_image, config_int, config_truthy, draw_drops_on_image,
        draw_leaderboard, load_background_image,
    )

    group = session.query(Group).filter(Group.group_id == int(group_id)).first()
    if group is None:
        return None
    config = {
        row.config_key: row.config_value
        for row in session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == int(group_id)
        ).all()
    }
    style_id = config_int(config.get("pvp_loot_board_type"), 0) or config_int(
        config.get("loot_board_type"), 1) or 1
    style = (session.query(LootboardStyle).filter(LootboardStyle.id == style_id).first()
             or session.query(LootboardStyle).filter(LootboardStyle.id == 1).first())
    background = style.local_url if style else "/store/droptracker/disc/lootboard/themes/bank-new-clean-dark.png"
    min_value = config_int(config.get("pvp_minimum_value_to_notify"), 1_000_000)
    dynamic_colors = config_truthy(
        config.get("use_dynamic_colors", config.get("use_dynamic_lootboard_colors")), default=True
    )
    use_gp = config_truthy(config.get("use_gp_colors"), default=True)

    partition = int(partition or _partition_now())
    group_items, player_totals, recent, total = collect_board_data(session, group_id, partition)

    ids = set(player_totals) | {r["player_id"] for r in recent[:RECENT_SLOTS]}
    names = {
        int(pid): name for pid, name in (
            session.query(Player.player_id, Player.player_name)
            .filter(Player.player_id.in_(ids)).all()
        )
    } if ids else {}

    month = calendar.month_name[partition % 100]
    if int(group_id) == GLOBAL_GROUP_ID:
        title = f"PvP Loot - All Players ({month})"
    else:
        title = f"{group.group_name}'s PvP Loot for {month}"

    bg_img, draw = load_background_image(background)
    bg_img = await draw_drops_on_image(bg_img, draw, group_items, group_id,
                                       dynamic_colors=dynamic_colors, use_gp=use_gp)
    bg_img = await _draw_header(bg_img, draw, title, total, dynamic_colors=dynamic_colors)
    bg_img = await _draw_recent_kills(bg_img, draw, recent, names, min_value,
                                      dynamic_colors=dynamic_colors)
    bg_img = await draw_leaderboard(bg_img, draw, player_totals, dynamic_colors=dynamic_colors,
                                    use_gp=use_gp, session_to_use=session)

    path = board_path(group_id)
    _ensure_public_dir(path.rsplit("/", 1)[0])
    _save_public_image(bg_img, path)
    return path
