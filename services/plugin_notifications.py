"""Event → plugin in-game notification inbox (P0 of the plan in
docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md).

Per-player Redis inbox (``plugin:notify:{player_id}``) drained by
``GET /notifications`` on the intake API. Producers:

- ``services/event_engine._enqueue_notification`` → :func:`fan_out_event_notification`
  (called BEFORE the Discord message_config mute gate — a player's in-game
  notifications are independent of the event's Discord verbosity config),
- ``workers/webhook_consumer`` → :func:`push_submission_notice` (restores the
  ``/webhook`` response ``notice`` channel that queue mode silenced).

Safety contract: inbox entries are typed data only —
``{id, type, ts, event?, data}``. The server never sends display strings or
markup for event types; the plugin owns rendering via a hardcoded
type→renderer registry and silently drops unknown types. The one exception is
``submission_notice`` (legacy processor notice text), which the plugin renders
as sanitized plain chat gated by its existing receiveInGameMessages toggle.

Per-type website prefs (``player_notification_prefs``) are enforced here at
delivery time: a disabled type never enters the inbox. ``event_task_progress``
is deliberately NOT web-configurable — it is always delivered and filtered by
a plugin-side config toggle (the mute switch for the noisiest type lives in
game, and no option may exist in two places).

Module-level imports are stdlib-only on purpose (the unit tests load this file
directly and the conftest stubs ``db``/``utils``); anything DB- or
Redis-shaped is lazy-imported inside functions. All Redis delivery is
best-effort: a Redis outage must never break event processing or submission
intake.
"""
from __future__ import annotations

import json
import time
import uuid

INBOX_KEY_TEMPLATE = "plugin:notify:{player_id}"
INBOX_CAP = 50
INBOX_TTL_SECONDS = 24 * 3600
DRAIN_BATCH_LIMIT = 25
# Long-poll wake channel: every inbox push publishes the player_id here so a
# held ``GET /notifications?wait=N`` returns immediately instead of waiting
# out its poll timer. Mirrored as a literal in api/notify_wake.py (the API
# process must not import services.* at module scope — test conftest stubs it).
WAKE_CHANNEL = "plugin:notify:wake"
# Free-form notice text is capped defensively; the plugin caps again on render.
NOTICE_MAX_CHARS = 500

# "What is this player working toward" stamp (P0.5): written at apply time
# when the player's own submission advances an incomplete task; read by
# /event_state to headline the HUD. A few hours is long enough to survive a
# session, short enough to fall back to team progress when they move on.
FOCUS_KEY_TEMPLATE = "plugin:focus:{player_id}:{event_id}"
FOCUS_TTL_SECONDS = 6 * 3600

# Public /img host (same values notification_service uses for Discord
# thumbnails) — used for focus-task icon URLs when the icon is not an item
# (NPC/skill icons have no client-side sprite).
IMG_BASE = "https://www.droptracker.io/img"
STATIC_IMG_DIR = "/store/droptracker/disc/static/assets/img"

AUDIENCE_TEAM = "team"
AUDIENCE_EVENT = "event"

# /event_state per-entry list caps: the plugin renders a task picker and a
# roster inline in a 225px side panel — bound the payload, don't paginate.
TASKS_LIMIT = 60
REQUIREMENTS_LIMIT = 12
MEMBERS_LIMIT = 60

# Which notification_queue event types are delivered in-game, and to whom.
# Types absent here (event_pending, event_activation_failed,
# event_signup_prompt, event_pot) are Discord admin/announcement concerns
# with no in-game audience.
AUDIENCE_FOR_TYPE = {
    "event_completion": AUDIENCE_TEAM,
    "event_task_progress": AUDIENCE_TEAM,
    "event_line": AUDIENCE_TEAM,
    "event_blackout": AUDIENCE_TEAM,
    "event_board_turn": AUDIENCE_TEAM,
    "event_board_roll_prompt": AUDIENCE_TEAM,
    "event_lead_change": AUDIENCE_EVENT,
    "event_started": AUDIENCE_EVENT,
    "event_ended": AUDIENCE_EVENT,
}

# Types a player may switch off on the website (P1 UI writes
# player_notification_prefs). Absent-from-prefs means enabled — defaults are
# all-on. event_task_progress is client-toggle-only by design (see module
# docstring); submission_notice is gated by the plugin's existing
# receiveInGameMessages config.
WEB_PREF_TYPES = (
    "event_completion",
    "event_line",
    "event_blackout",
    "event_board_turn",
    "event_board_roll_prompt",
    "event_lead_change",
    "event_started",
    "event_ended",
)


def _redis():
    """Raw redis handle (the RedisClient wrapper exposes no pipeline/list-trim ops)."""
    from utils.redis import RedisClient

    return RedisClient().client


def _inbox_key(player_id) -> str:
    return INBOX_KEY_TEMPLATE.format(player_id=int(player_id))


def build_envelope(notification_type: str, data: dict, event: dict = None,
                   now: int = None) -> dict:
    """The typed wire envelope. Versionless by design: future needs are new
    ``type`` strings (which unaware plugins drop), never mutations of existing
    ones."""
    ts = int(now if now is not None else time.time())
    envelope = {
        "id": f"{ts}-{uuid.uuid4().hex[:8]}",
        "type": notification_type,
        "ts": ts,
        "data": dict(data or {}),
    }
    if isinstance(event, dict) and event.get("id") is not None:
        envelope["event"] = {"id": event.get("id"), "name": event.get("name")}
    return envelope


def push_to_inbox(player_id, envelope: dict) -> bool:
    """Best-effort append to one player's inbox (capped, TTL-refreshed)."""
    if not player_id:
        return False
    try:
        serialized = json.dumps(envelope, default=str)
        key = _inbox_key(player_id)
        pipe = _redis().pipeline()
        pipe.rpush(key, serialized)
        pipe.ltrim(key, -INBOX_CAP, -1)
        pipe.expire(key, INBOX_TTL_SECONDS)
        pipe.publish(WAKE_CHANNEL, str(int(player_id)))
        pipe.execute()
        return True
    except Exception as e:
        print(f"[plugin_notifications] inbox push failed for player {player_id}: {e}")
        return False


def drain_inbox(player_id, limit: int = DRAIN_BATCH_LIMIT) -> list:
    """Pop up to ``limit`` entries (FIFO). LRANGE+LTRIM run in one MULTI/EXEC
    pipeline; the only reader of a given inbox is that player's own plugin, so
    there is no competing consumer to race."""
    if not player_id:
        return []
    key = _inbox_key(player_id)
    try:
        pipe = _redis().pipeline()
        pipe.lrange(key, 0, int(limit) - 1)
        pipe.ltrim(key, int(limit), -1)
        raw_items = pipe.execute()[0] or []
    except Exception as e:
        print(f"[plugin_notifications] inbox drain failed for player {player_id}: {e}")
        return []
    entries = []
    for item in raw_items:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            entries.append(json.loads(item))
        except Exception:
            continue
    return entries


def push_submission_notice(player_id, message) -> bool:
    """Deliver a processor notice (the pre-queue-mode /webhook ``notice``) to
    the submitting player's inbox."""
    if not player_id or not message:
        return False
    data = {"message": str(message)[:NOTICE_MAX_CHARS]}
    return push_to_inbox(player_id, build_envelope("submission_notice", data))


def stamp_player_focus(player_id, event_id, task_id) -> None:
    """Record the task this player's own submission just advanced. Best-effort."""
    if not player_id or not event_id or not task_id:
        return
    try:
        key = FOCUS_KEY_TEMPLATE.format(player_id=int(player_id),
                                        event_id=int(event_id))
        _redis().setex(key, FOCUS_TTL_SECONDS, str(int(task_id)))
    except Exception:
        pass


def _stamped_focus_task_id(player_id, event_id):
    try:
        raw = _redis().get(FOCUS_KEY_TEMPLATE.format(
            player_id=int(player_id), event_id=int(event_id)))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return int(raw)
    except Exception:
        return None


def resolve_item_icon_id(session, item_name):
    """Game item id for an item name (un-noted, lowest id), or None. The
    client renders the sprite locally via ItemManager — no image fetch."""
    if not item_name:
        return None
    try:
        from sqlalchemy import func

        from db.models import ItemList

        iid = (session.query(func.min(ItemList.item_id))
               .filter(ItemList.item_name == str(item_name),
                       ItemList.noted.is_(False))
               .scalar())
        return int(iid) if iid is not None else None
    except Exception:
        return None


def pick_focus_task(tasks, progress_by_task, stamped_task_id=None):
    """Choose the task to headline the HUD. Pure logic (unit-tested).

    ``tasks``: list of {"id", "label", "type", "target_value"} dicts.
    ``progress_by_task``: {task_id: {"progress": int, "completed": bool}}.

    Order: the stamped (player-inferred) task while it is still incomplete;
    else the most-progressed incomplete task (completion ratio, ties to the
    lower id); else the first task with no progress at all; else None (team
    finished everything). Returns (task_dict, source) or (None, None).
    """
    by_id = {t["id"]: t for t in tasks}

    def _completed(task_id):
        return bool((progress_by_task.get(task_id) or {}).get("completed"))

    if stamped_task_id is not None and stamped_task_id in by_id \
            and not _completed(stamped_task_id):
        return by_id[stamped_task_id], "inferred"

    best = None
    best_ratio = 0.0
    for task in tasks:
        state = progress_by_task.get(task["id"]) or {}
        if state.get("completed"):
            continue
        progress = int(state.get("progress") or 0)
        if progress <= 0:
            continue
        need = max(int(task.get("target_value") or 0), 1)
        if task.get("type") in ("pb_target", "skill_target"):
            need = 1
        ratio = progress / need
        if best is None or ratio > best_ratio \
                or (ratio == best_ratio and task["id"] < best["id"]):
            best = task
            best_ratio = ratio
    if best is not None:
        return best, "team_progress"

    for task in tasks:
        if not _completed(task["id"]):
            return task, "first_task"
    return None, None


def _task_icon(session, task_row):
    """(icon_item_id, icon_url) for an event task, via the site's task-tile
    derivation (the same source Discord thumbnails use). Prefers an item id
    (client renders the sprite locally); falls back to an /img URL for
    NPC/skill icons whose asset exists on disk. (None, None) when nothing
    resolves. Never raises."""
    import os

    try:
        from sqlalchemy import func

        from db.models import ItemList, NpcList
        from web_api.task_tiles import (
            build_tile,
            icon_asset_path,
            spec_names,
            tile_spec,
        )

        spec = tile_spec({
            "id": task_row.id, "type": task_row.type, "label": task_row.label,
            "target": task_row.target, "target_value": task_row.target_value,
            "config": task_row.config,
        })
        item_names, npc_names = spec_names(spec)
        item_ids: dict = {}
        if item_names:
            for iid, name in (
                session.query(func.min(ItemList.item_id), ItemList.item_name)
                .filter(ItemList.item_name.in_(item_names), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .all()
            ):
                item_ids[" ".join(name.strip().lower().split())] = iid
        npc_ids: dict = {}
        if npc_names:
            for nid, name in (
                session.query(func.min(NpcList.npc_id), NpcList.npc_name)
                .filter(NpcList.npc_name.in_(npc_names))
                .group_by(NpcList.npc_name)
                .all()
            ):
                npc_ids[" ".join(name.strip().lower().split())] = nid
        tile = build_tile(spec, item_ids, npc_ids)
        for icon in tile.get("icons") or []:
            if icon.get("type") == "item" and icon.get("id"):
                return int(icon["id"]), None
        for icon in tile.get("icons") or []:
            rel = icon_asset_path(icon)
            if rel and os.path.exists(os.path.join(STATIC_IMG_DIR, rel)):
                return None, f"{IMG_BASE}/{rel}"
    except Exception:
        pass
    return None, None


def _requirement_entries(config: dict) -> list:
    """Ordered, de-duplicated item requirements from an item_collection
    config, keeping per-item ``quantity`` and ``points`` (the fields
    ``web_api.task_tiles._config_items`` drops). Pure."""
    out: list = []
    seen: set = set()

    def _add(entry) -> None:
        if isinstance(entry, str):
            name, qty, pts = entry, None, None
        elif isinstance(entry, dict):
            name = entry.get("item_name") or entry.get("name")
            qty = entry.get("quantity")
            pts = entry.get("points")
        else:
            return
        key = " ".join(str(name or "").strip().lower().split())
        if not key or key in seen:
            return
        seen.add(key)
        row = {"name": str(name).strip()}
        try:
            if qty is not None and int(qty) > 1:
                row["quantity"] = int(qty)
        except (TypeError, ValueError):
            pass
        try:
            if pts is not None:
                row["points"] = int(float(pts))
        except (TypeError, ValueError):
            pass
        out.append(row)

    for it in config.get("items") or []:
        _add(it)
    for it in config.get("any_of") or []:
        _add(it)
    for group in config.get("groups") or []:
        if isinstance(group, dict):
            for it in group.get("items") or []:
                _add(it)
    for path in config.get("paths") or []:
        if isinstance(path, dict):
            for group in path.get("groups") or []:
                if isinstance(group, dict):
                    for it in group.get("items") or []:
                        _add(it)
            for it in path.get("items") or []:  # points path: weighted item list
                _add(it)
    return out


def _norm_name(value) -> str:
    """Case/whitespace-insensitive item-name key (mirrors event_engine._norm)."""
    return " ".join(str(value or "").strip().lower().split())


def mark_obtained_requirements(requirements: list, config: dict,
                               collected_names: set) -> list:
    """Annotate requirement entries with ``"obtained": True`` when the team
    has already banked that item AND re-receiving it can no longer advance
    the task — the plugin strikes those lines through in task tooltips.

    Eligible kinds: ``all_of``/``assembly`` (every listed item counts once),
    and ``groups`` for items inside all_of-mode groups. ``any_of``
    re-receives still fold into progress and ``point_collection`` items are
    re-earnable for points, so those are never annotated. ``collected_names``
    is a set of normalized ``EventCompletion.matched_target`` values.
    Mutates and returns ``requirements``. Pure.
    """
    if not requirements or not collected_names:
        return requirements
    kind = (config.get("kind") if isinstance(config, dict) else None) or "any_of"
    if kind in ("all_of", "assembly"):
        eligible = None  # every listed item
    elif kind == "groups":
        eligible = set()
        for group in (config.get("groups") or []):
            if not isinstance(group, dict):
                continue
            mode = group.get("mode") if group.get("mode") in ("all_of", "any_of") else "all_of"
            if mode != "all_of":
                continue
            for it in group.get("items") or []:
                name = it if isinstance(it, str) \
                    else (it or {}).get("item_name") or (it or {}).get("name")
                key = _norm_name(name)
                if key:
                    eligible.add(key)
    else:
        return requirements
    for req in requirements:
        key = _norm_name(req.get("name"))
        if key in collected_names and (eligible is None or key in eligible):
            req["obtained"] = True
    return requirements


def describe_task(task: dict) -> tuple:
    """(description, requirements) explaining one event task to the plugin.

    ``task``: {"type", "label", "target", "target_value", "config"} (the
    EventTask columns). The sentence is composed server-side so every client
    renders identical wording; ``requirements`` is the capped per-item list
    ([{"name", "quantity"?, "points"?}]) for item-list tasks, else []. Pure.
    """
    from web_api.task_tiles import _fmt_num, _fmt_time, _parse_config

    task_type = task.get("type")
    target = (task.get("target") or "").strip()
    tv = task.get("target_value")
    config = _parse_config(task.get("config"))

    if task_type == "item_collection":
        reqs = _requirement_entries(config)
        total = len(reqs)
        capped = reqs[:REQUIREMENTS_LIMIT]
        extra = f" (+{total - len(capped)} more)" if total > len(capped) else ""
        # Metric alternatives of an any_path config ("boss pet OR 5,000 GWD
        # kills") — summarized into the sentence; they carry no item rows.
        metric_bits = []
        for path in config.get("paths") or []:
            if not isinstance(path, dict):
                continue
            if path.get("kind") == "points":
                # Points path ("Full set OR 500 pts of listed items") — its
                # weighted items ride in the shared requirements list above.
                metric_bits.append(f"{_fmt_num(path.get('need'))} pts of listed items")
                continue
            if not path.get("metric"):
                continue
            npcs = [str(n).strip() for n in (path.get("npcs") or []) if str(n).strip()]
            at = f" at {', '.join(npcs[:4])}" if npcs else ""
            if path["metric"] == "kc":
                metric_bits.append(f"{_fmt_num(path.get('need'))} kills{at}")
            else:
                metric_bits.append(f"{_fmt_num(path.get('need'))} GP of drops{at}")
        if not reqs and not metric_bits:
            if target and isinstance(tv, int) and tv > 1:
                return f"Obtain {tv}× {target}.", []
            if target:
                return f"Obtain {target}.", []
            return None, []
        kind = config.get("kind") or "any_of"
        if kind == "point_collection":
            desc = (f"Earn {_fmt_num(tv)} points. Each listed item awards "
                    f"its own point value{extra}.")
        elif kind == "all_of":
            desc = f"Collect every one of the {total} listed items{extra}."
        elif kind == "assembly":
            desc = f"Assemble the complete set: {total} pieces{extra}."
        elif kind == "groups":
            desc = f"Complete every listed item requirement{extra}."
        elif kind == "any_path":
            ors = " OR ".join(metric_bits)
            if reqs and metric_bits:
                desc = f"Complete any one path: the listed items OR {ors}{extra}."
            elif metric_bits:
                desc = f"Complete any one path: {ors}."
            else:
                desc = f"Complete any one of the listed item paths{extra}."
        elif isinstance(tv, int) and tv > 1:
            desc = f"Collect any {tv} of the {total} listed items{extra}."
        else:
            desc = f"Collect any one of the {total} listed items{extra}."
        return desc, capped

    if task_type == "kc_target":
        return (f"Reach {_fmt_num(tv)} kills at {target or 'the target boss'}.", [])
    if task_type == "pb_target":
        boss = target or "the target boss"
        time_s = _fmt_time(tv)
        mode = config.get("mode")
        try:
            need = int(config.get("need") or 1)
        except (TypeError, ValueError):
            need = 1
        if mode == "whole_team":
            return (f"Every player on the team must beat {time_s} at {boss}.", [])
        if mode == "unique_players":
            return (f"{need} different players must each beat {time_s} at {boss}.", [])
        if mode == "times" and need > 1:
            return (f"Beat {time_s} at {boss} {need} times — repeat kills count.", [])
        return (f"Beat a personal best of {time_s} at {boss}.", [])
    if task_type == "xp_target":
        skill = target.capitalize() if target else "the target skill"
        return f"Gain {_fmt_num(tv)} {skill} XP as a team.", []
    if task_type == "skill_target":
        skill = target.capitalize() if target else "the target skill"
        return f"Reach level {tv} {skill} on one account.", []
    if task_type == "loot_value":
        sources = [str(n).strip() for n in (config.get("source_npcs") or []) if str(n).strip()]
        if target and target not in sources:
            sources.append(target)
        scope = f" from {', '.join(sources[:6])}" if sources else ""
        return f"Loot {_fmt_num(tv)} GP worth of drops{scope}.", []
    if task_type == "ehp_target":
        return f"Gain {_fmt_num(tv)} EHP (efficient hours played).", []
    if task_type == "ehb_target":
        return f"Gain {_fmt_num(tv)} EHB (efficient hours bossed).", []
    return None, []


def _batch_task_tiles(session, task_rows) -> dict:
    """{task_id: {"icon_item_id", "icon_url", "badge", "value"}} for a whole
    event's tasks, resolving item/NPC names in two bulk queries (the per-task
    variant of this is :func:`_task_icon`; a 30-task bingo board must not run
    60 lookups per /event_state). Never raises."""
    import os

    tiles: dict = {}
    try:
        from sqlalchemy import func

        from db.models import ItemList, NpcList
        from web_api.task_tiles import (
            build_tile,
            icon_asset_path,
            spec_names,
            tile_spec,
        )

        specs = {}
        item_names: set = set()
        npc_names: set = set()
        for task_row in task_rows:
            try:
                spec = tile_spec({
                    "id": task_row.id, "type": task_row.type,
                    "label": task_row.label, "target": task_row.target,
                    "target_value": task_row.target_value,
                    "config": task_row.config,
                })
            except Exception:
                continue
            specs[task_row.id] = spec
            items, npcs = spec_names(spec)
            item_names |= items
            npc_names |= npcs

        item_ids: dict = {}
        if item_names:
            for iid, name in (
                session.query(func.min(ItemList.item_id), ItemList.item_name)
                .filter(ItemList.item_name.in_(item_names), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .all()
            ):
                item_ids[" ".join(name.strip().lower().split())] = iid
        npc_ids: dict = {}
        if npc_names:
            for nid, name in (
                session.query(func.min(NpcList.npc_id), NpcList.npc_name)
                .filter(NpcList.npc_name.in_(npc_names))
                .group_by(NpcList.npc_name)
                .all()
            ):
                npc_ids[" ".join(name.strip().lower().split())] = nid

        for task_id, spec in specs.items():
            tile = build_tile(spec, item_ids, npc_ids)
            icon_item_id = None
            icon_url = None
            for icon in tile.get("icons") or []:
                if icon.get("type") == "item" and icon.get("id"):
                    icon_item_id = int(icon["id"])
                    break
            if icon_item_id is None:
                for icon in tile.get("icons") or []:
                    rel = icon_asset_path(icon)
                    if rel and os.path.exists(os.path.join(STATIC_IMG_DIR, rel)):
                        icon_url = f"{IMG_BASE}/{rel}"
                        break
            tiles[task_id] = {
                "icon_item_id": icon_item_id,
                "icon_url": icon_url,
                "badge": tile.get("badge"),
                "value": tile.get("value"),
            }
    except Exception as e:
        print(f"[plugin_notifications] batch tile resolve failed: {e}")
    return tiles


def _task_screenshot_names(task_type, target, config: dict) -> set:
    """Normalized item names a task can be credited by — the pool the plugin
    force-screenshots for proof while the task is incomplete. Pure; config is
    an already-parsed dict. Only item-bearing task types contribute:

    - ``item_collection``: the single ``target`` plus every listed name
      (``items`` / ``any_of`` / ``groups`` / ``paths``),
    - ``loot_sweep``: every drop-sourced matcher key (pet entries credit from
      pet submissions, which carry their own screenshot config).
    """
    names: set = set()
    try:
        if task_type == "item_collection":
            from services.event_engine import _config_item_entries, _norm

            names.update(_config_item_entries(config or {}).keys())
            target_norm = _norm(target)
            if target_norm:
                names.add(target_norm)
        elif task_type == "loot_sweep":
            from services.loot_sweep import LootSweepConfig

            index = LootSweepConfig(config or {}).matcher_index()
            names.update(k for k, v in index.items()
                         if (v or {}).get("source") == "drop")
    except Exception as e:
        print(f"[plugin_notifications] screenshot-name extract failed: {e}")
    names.discard("")
    return names


def _resolve_item_ids(session, names: set) -> list:
    """Item ids for a set of normalized item names (all variants of a name —
    noted/duplicate ids included, so the dropped id always matches client-side).
    MySQL's case-insensitive collation lets the indexed ``IN`` do the matching."""
    if not names:
        return []
    from db.models import ItemList

    rows = (
        session.query(ItemList.item_id)
        .filter(ItemList.item_name.in_(sorted(names)))
        .all()
    )
    return sorted({int(r[0]) for r in rows})


def compose_event_state(session, player_id) -> dict:
    """The plugin's HUD/Events-tab state: one entry per active event the
    player is rostered in. All composition happens here so the client stays a
    dumb renderer of typed fields."""
    from db.models import Event, EventProgress, EventTask, EventTeam, EventTeamMember

    rows = (
        session.query(EventTeamMember, EventTeam, Event)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .join(Event, Event.id == EventTeam.event_id)
        .filter(EventTeamMember.player_id == int(player_id),
                Event.status == "active")
        .order_by(Event.activated_at.desc(), Event.id.desc())
        .all()
    )

    entries = []
    screenshot_names: set = set()
    for _member, team, event in rows:
        teams = (
            session.query(EventTeam)
            .filter(EventTeam.event_id == event.id)
            .order_by(EventTeam.score.desc(), EventTeam.id.asc())
            .all()
        )
        standings = []
        team_rank = None
        for idx, t in enumerate(teams, start=1):
            if t.id == team.id:
                team_rank = idx
            standings.append({
                "team_id": t.id,
                "name": t.name,
                "score": int(t.score or 0),
                "rank": idx,
                "color": t.color,
            })

        task_rows = (
            session.query(EventTask)
            .filter(EventTask.event_id == event.id)
            .order_by(EventTask.id.asc())
            .all()
        )
        progress_rows = (
            session.query(EventProgress)
            .filter(EventProgress.event_id == event.id,
                    EventProgress.team_id == team.id)
            .all()
        )
        progress_by_task = {
            p.task_id: {"progress": int(p.progress or 0),
                        "completed": bool(p.completed)}
            for p in progress_rows
        }
        tasks_total = len(task_rows)
        tasks_completed = sum(1 for p in progress_rows if p.completed)
        tiles = _batch_task_tiles(session, task_rows)

        board_status = None
        focus_row = None
        focus_source = None
        if event.kind == "board_game":
            # Board game: the team's current tile task IS the focus.
            from db.models import EventBoardPosition

            position = (
                session.query(EventBoardPosition)
                .filter(EventBoardPosition.event_id == event.id,
                        EventBoardPosition.team_id == team.id)
                .first()
            )
            if position is not None:
                board_status = position.status
                if position.current_task_id:
                    focus_row = (
                        session.query(EventTask)
                        .filter(EventTask.id == position.current_task_id)
                        .first()
                    )
                    focus_source = "board"
        if focus_row is None:
            task_dicts = [
                {"id": t.id, "label": t.label, "type": t.type,
                 "target_value": t.target_value}
                for t in task_rows
            ]
            stamped = _stamped_focus_task_id(player_id, event.id)
            picked, focus_source = pick_focus_task(
                task_dicts, progress_by_task, stamped)
            if picked is not None:
                focus_row = next(
                    (t for t in task_rows if t.id == picked["id"]), None)

        focus_task = None
        if focus_row is not None:
            state = progress_by_task.get(focus_row.id) or {}
            need = 1 if focus_row.type in ("pb_target", "skill_target") \
                else max(int(focus_row.target_value or 0), 1)
            focus_tile = tiles.get(focus_row.id)
            if focus_tile is not None:
                icon_item_id = focus_tile.get("icon_item_id")
                icon_url = focus_tile.get("icon_url")
            else:
                icon_item_id, icon_url = _task_icon(session, focus_row)
            focus_task = {
                "id": focus_row.id,
                "label": focus_row.label,
                "have": int(state.get("progress") or 0),
                "need": need,
                "icon_item_id": icon_item_id,
                "icon_url": icon_url,
                "source": focus_source,
            }

        # Which listed items the team has already banked, per task — drives
        # the struck-through requirement lines in plugin tooltips. One bulk
        # query; bonus rows carry no matched_target so the NULL filter drops
        # them.
        collected_by_task: dict = {}
        try:
            from db.models import EventCompletion

            listed_ids = [t.id for t in task_rows[:TASKS_LIMIT]]
            if listed_ids:
                credited = (
                    session.query(EventCompletion.task_id,
                                  EventCompletion.matched_target)
                    .filter(EventCompletion.task_id.in_(listed_ids),
                            EventCompletion.team_id == team.id,
                            EventCompletion.status.in_(("auto", "confirmed", "manual")),
                            EventCompletion.matched_target.isnot(None))
                    .all()
                )
                for credited_task_id, credited_target in credited:
                    collected_by_task.setdefault(credited_task_id, set()).add(
                        _norm_name(credited_target))
        except Exception as e:
            print(f"[plugin_notifications] credited-item lookup failed: {e}")

        # Items the plugin should force-screenshot for proof: everything an
        # incomplete task can still be credited by, minus what the team has
        # already banked for that task. Resolved to ids once, after all events.
        for task_row in task_rows:
            state = progress_by_task.get(task_row.id) or {}
            if state.get("completed"):
                continue
            try:
                raw_config = task_row.config
                if isinstance(raw_config, dict):
                    task_config = raw_config
                elif raw_config:
                    task_config = json.loads(raw_config)
                else:
                    task_config = {}
                if not isinstance(task_config, dict):
                    task_config = {}
            except Exception:
                task_config = {}
            screenshot_names.update(
                _task_screenshot_names(task_row.type, task_row.target, task_config)
                - (collected_by_task.get(task_row.id) or set()))

        # The full task list: powers the plugin's task picker (click a task to
        # track it on the HUD instead of the server's focus pick), tooltips
        # explaining each task's requirements, and per-task progress rows.
        tasks_payload = []
        for task_row in task_rows[:TASKS_LIMIT]:
            state = progress_by_task.get(task_row.id) or {}
            need = 1 if task_row.type in ("pb_target", "skill_target") \
                else max(int(task_row.target_value or 0), 1)
            try:
                description, requirements = describe_task({
                    "type": task_row.type, "label": task_row.label,
                    "target": task_row.target,
                    "target_value": task_row.target_value,
                    "config": task_row.config,
                })
            except Exception:
                description, requirements = None, []
            try:
                raw_config = task_row.config
                if isinstance(raw_config, dict):
                    task_config = raw_config
                elif raw_config:
                    task_config = json.loads(raw_config)
                else:
                    task_config = {}
                if not isinstance(task_config, dict):
                    task_config = {}
                mark_obtained_requirements(
                    requirements, task_config,
                    collected_by_task.get(task_row.id) or set())
            except Exception:
                pass
            tile = tiles.get(task_row.id) or {}
            tasks_payload.append({
                "id": task_row.id,
                "label": task_row.label,
                "type": task_row.type,
                "points": int(task_row.points or 0),
                "have": int(state.get("progress") or 0),
                "need": need,
                "completed": bool(state.get("completed")),
                "icon_item_id": tile.get("icon_item_id"),
                "icon_url": tile.get("icon_url"),
                "badge": tile.get("badge"),
                "value": tile.get("value"),
                "description": description,
                "requirements": requirements,
            })

        # Own-team roster for the Events tab (names only; capped, with the
        # real total so the client can render "… and N more").
        members = []
        members_total = 0
        try:
            from db.models import Player

            members_total = (
                session.query(EventTeamMember.player_id)
                .filter(EventTeamMember.team_id == team.id)
                .count()
            )
            member_rows = (
                session.query(EventTeamMember.player_id, Player.player_name)
                .outerjoin(Player, Player.player_id == EventTeamMember.player_id)
                .filter(EventTeamMember.team_id == team.id)
                .order_by(Player.player_name.asc())
                .limit(MEMBERS_LIMIT)
                .all()
            )
            members = [
                {"player_id": pid, "name": name or f"Player {pid}"}
                for pid, name in member_rows
            ]
        except Exception as e:
            print(f"[plugin_notifications] roster lookup failed: {e}")

        entries.append({
            "event": {"id": event.id, "name": event.name, "kind": event.kind,
                      "has_bingo": bool(event.has_bingo),
                      "ends_at": event.ends_at.isoformat() if event.ends_at else None},
            "team": {
                "id": team.id,
                "name": team.name,
                "color": team.color,
                "icon_item_id": team.piece_item_id,
                "icon_url": team.piece_icon_url,
                "score": int(team.score or 0),
                "rank": team_rank,
                "team_count": len(teams),
            },
            "focus_task": focus_task,
            "board_status": board_status,
            "tasks_completed": tasks_completed,
            "tasks_total": tasks_total,
            "tasks": tasks_payload,
            "members": members,
            "members_total": members_total,
            "board": {
                "available": bool(event.has_bingo or event.kind == "board_game"),
                "team_id": team.id,
            },
            "standings": standings,
        })

    screenshot_item_ids: list = []
    try:
        screenshot_item_ids = _resolve_item_ids(session, screenshot_names)
    except Exception as e:
        print(f"[plugin_notifications] screenshot-id resolve failed: {e}")
    return {"events": entries, "screenshot_item_ids": screenshot_item_ids}


def player_has_active_event(session, player_id) -> bool:
    """True when the player is on a team roster of a live event — the plugin's
    signal to start polling GET /notifications (analogue of track_xp_events)."""
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT 1 FROM web_event_team_members m "
            "JOIN web_event_teams t ON t.id = m.team_id "
            "JOIN web_events e ON e.id = t.event_id "
            "WHERE m.player_id = :player_id AND e.status = 'active' LIMIT 1"
        ),
        {"player_id": int(player_id)},
    ).first()
    return row is not None


def _team_player_ids(session, team_id) -> list:
    from db.models.events import EventTeamMember

    rows = (
        session.query(EventTeamMember.player_id)
        .filter(EventTeamMember.team_id == int(team_id))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _event_player_ids(session, event_id) -> list:
    from db.models.events import EventTeam, EventTeamMember

    rows = (
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == int(event_id))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _players_with_type_disabled(session, notification_type: str, player_ids) -> set:
    """Subset of player_ids whose stored website prefs disable this type.
    Missing rows / missing keys mean enabled (defaults are all-on)."""
    if notification_type not in WEB_PREF_TYPES or not player_ids:
        return set()
    # Fail-open: a prefs lookup failure (e.g. table not migrated yet) must
    # not kill delivery — defaults are all-on anyway.
    try:
        from db.models import PlayerNotificationPrefs

        rows = (
            session.query(PlayerNotificationPrefs)
            .filter(PlayerNotificationPrefs.player_id.in_(list(player_ids)))
            .all()
        )
    except Exception as e:
        print(f"[plugin_notifications] prefs lookup failed (delivering with defaults): {e}")
        return set()

    disabled = set()
    for row in rows:
        try:
            prefs = json.loads(row.prefs or "{}")
        except Exception:
            continue
        if prefs.get(notification_type) is False:
            disabled.add(row.player_id)
    return disabled


def fan_out_event_notification(session, notification_type: str, event: dict,
                               data: dict) -> int:
    """Deliver one event notification to every in-game recipient.

    The ``notification_queue`` row this mirrors carries only the *acting*
    player; the in-game audience is the whole team (or all event
    participants), resolved here at delivery time. Returns the number of
    inboxes pushed. Never raises — callers sit on the event-apply path.

    ``session`` is accepted for signature symmetry with the engine helpers but
    the reads run on a dedicated short-lived session: this is called
    mid-transaction on the event-apply session, and a failed read there
    (missing table, transient DB error) would poison the caller's transaction
    with a pending rollback. Rosters/prefs are written outside the apply
    transaction, so a fresh session always sees them.
    """
    try:
        audience = AUDIENCE_FOR_TYPE.get(notification_type)
        if audience is None:
            return 0
        event_id = event.get("id") if isinstance(event, dict) else None
        if audience == AUDIENCE_TEAM:
            team_id = (data or {}).get("team_id")
            if team_id is None:
                return 0
        elif event_id is None:
            return 0

        from db.models.base import Session

        read_session = Session()
        try:
            if audience == AUDIENCE_TEAM:
                player_ids = _team_player_ids(read_session, team_id)
            else:
                player_ids = _event_player_ids(read_session, event_id)
            if not player_ids:
                return 0
            disabled = _players_with_type_disabled(
                read_session, notification_type, player_ids)
            # Uniform enrichment: every envelope that names a team carries its
            # name, so client renderers never need a lookup (lead changes and
            # completions only carry team_id upstream).
            if (data or {}).get("team_id") is not None and not (data or {}).get("team_name"):
                from db.models.events import EventTeam

                team_row = (read_session.query(EventTeam.name)
                            .filter(EventTeam.id == int(data["team_id"]))
                            .first())
                if team_row:
                    data = dict(data)
                    data["team_name"] = team_row[0]
        finally:
            try:
                read_session.rollback()
            except Exception:
                pass
            read_session.close()

        envelope = build_envelope(notification_type, data, event=event)
        delivered = 0
        for pid in player_ids:
            if pid in disabled:
                continue
            if push_to_inbox(pid, envelope):
                delivered += 1
        return delivered
    except Exception as e:
        print(f"[plugin_notifications] fan-out failed for {notification_type}: {e}")
        return 0
