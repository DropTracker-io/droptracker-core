"""Group setup alerts (web138a): tell a group's owner when its DropTracker
setup breaks or stalls, instead of letting it fail silently.

Runs in the core bot (bots/main.py), because it DMs, and only a bot holds a
Discord connection.

What it watches, every 15 minutes:

  bot_missing       the group's server is gone from ``bot:guilds`` (the core
                    bot's own 5-minute REST list of its servers) on two checks
                    in a row
  posts_failing     in the last 24h, the group's notifications failed five or
                    more times on a Discord channel error and none went out
  no_channel        a group created since alerts started has no drops channel
                    24h after creation (setup is "soft required": skippable,
                    but flagged)
  install_no_group  the bot joined a server 24h ago and nobody created a group

Who hears about it: the group's owner (group_admins role 'owner'). If there is
no owner, or their DMs are closed, the Discord server owner is next, then the
group's admins. A server with no group goes straight
to the server owner. Two messages at most per incident: the first, then one
reminder. Fixing the problem resolves the incident, and a later relapse starts
a new one.

No mass DM on launch: a problem that already held when the detector first saw
it (a group that lost the bot before anything was recording) is stored as
``baseline`` and never messaged. Those groups get a one-off campaign the owner
approves. bot_missing only opens when a leave was actually observed, and
posts_failing only when the group posted to Discord in the last 30 days.

``SETUP_ALERTS_MODE``: off | log | send. Defaults to send on production and
log everywhere else, so a dev box with a copy of production's database can
never DM real people (the dev guild guard's fetch_user wrapper also blocks it).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from db.app_logger import AppLogger

app_logger = AppLogger()

SITE = (os.getenv("WEB_SITE_URL") or "https://www.droptracker.io").rstrip("/")
INVITE_PERMISSIONS = "268553232"  # pinned equal to services.guild_permissions
CLIENT_ID = (os.getenv("DISCORD_BOT_CLIENT_ID") or "").strip() or "1172933457010245762"

PREV_IDS_KEY = "bot:guilds:ids_prev"
SINCE_KEY = "setup_alerts:since"
EXCLUDED_GROUPS = {1, 2}  # template + the global group

MAX_SENDS = 2
REMIND_AFTER = {
    "bot_missing": timedelta(days=3),
    "posts_failing": timedelta(days=3),
    "no_channel": timedelta(days=3),
    "install_no_group": timedelta(days=3),
}
STALL_AFTER = timedelta(hours=24)
FAILS_TO_ALERT = 5
BOT_MISSING_STRIKES = 2

# Notification failures that mean "the channel is broken", not a blip.
CHANNEL_ERROR_MARKERS = (
    "unknown channel", "channel not found", "not found", "missing access",
    "missing permissions", "forbidden", "not a text channel", "snowflake",
)


def mode() -> str:
    raw = (os.getenv("SETUP_ALERTS_MODE") or "").strip().lower()
    if raw in ("off", "log", "send"):
        return raw
    live = (os.getenv("STATE") or os.getenv("STATUS") or "").strip().strip('"').lower()
    return "send" if live in ("live", "prod", "production") else "log"


# --- pure policy (unit-tested) ----------------------------------------------------


def diff_presence(prev: Optional[set], cur: set) -> tuple[set, set]:
    """(joined, left) between two bot:guilds snapshots. No previous snapshot
    means nothing can be inferred yet."""
    if prev is None:
        return set(), set()
    return cur - prev, prev - cur


def snapshot_trustworthy(prev: Optional[set], cur: set) -> bool:
    """A sudden collapse of the server list is an enumeration problem, not
    the bot being kicked from half its servers at once."""
    if prev is None or len(prev) < 20:
        return True
    return len(cur) >= len(prev) * 0.8


def is_channel_error(message: Optional[str]) -> bool:
    text = (message or "").lower()
    return any(m in text for m in CHANNEL_ERROR_MARKERS)


@dataclass
class AlertRow:
    state: str  # open | baseline | resolved
    send_count: int
    last_sent_at: Optional[datetime]


def should_send(kind: str, row: AlertRow, now: datetime) -> bool:
    if row.state != "open" or row.send_count >= MAX_SENDS:
        return False
    if row.send_count == 0 or row.last_sent_at is None:
        return True
    return now - row.last_sent_at >= REMIND_AFTER[kind]


def recipient_order(owner: Optional[str], guild_owner: Optional[str], admins: Iterable[str]) -> list[str]:
    """Owner first; the Discord server owner if the group owner can't be
    reached; the group's admins after that. De-duplicated, order kept."""
    out: list[str] = []
    for d in [owner, guild_owner, *admins]:
        if d and d not in out:
            out.append(d)
    return out


def invite_url(guild_id: Optional[str]) -> str:
    q = f"client_id={CLIENT_ID}&permissions={INVITE_PERMISSIONS}&scope=bot+applications.commands"
    if guild_id:
        q += f"&guild_id={guild_id}&disable_guild_select=true"
    return f"https://discord.com/oauth2/authorize?{q}"


def build_message(kind: str, *, group_id: Optional[int], group_name: Optional[str],
                  guild_id: Optional[str], guild_name: Optional[str], detail: Optional[str],
                  reminder: bool) -> str:
    """Plain, short DM copy (site copy rules: no em-dashes, say what to do)."""
    name = f"**{group_name}**" if group_name else "your group"
    server = f"**{guild_name}**" if guild_name else "your Discord server"
    checklist = f"{SITE}/groups/{group_id}/admin" if group_id else None
    head = "Reminder: " if reminder else ""
    if kind == "bot_missing":
        body = (
            f"{head}The DropTracker bot is no longer in {server}, so {name} can't post drops, "
            f"personal bests or the lootboard to Discord. Your members' loot is still being tracked.\n\n"
            f"Add the bot back: {invite_url(guild_id)}\nSetup checklist: {checklist}"
        )
    elif kind == "posts_failing":
        reason = f" Discord said: {detail}." if detail else ""
        body = (
            f"{head}DropTracker couldn't post {name}'s notifications to Discord in the last day.{reason}\n\n"
            f"Check that the bot can see and post in your drops channel, or pick another one: "
            f"{SITE}/groups/{group_id}/settings\nSetup checklist: {checklist}"
        )
    elif kind == "no_channel":
        body = (
            f"{head}{name} is set up on DropTracker, but no drops channel is chosen yet, so nothing "
            f"posts to Discord.\n\nPick one here: {SITE}/groups/{group_id}/settings "
            f"(or run /group-setup in {server})."
        )
    else:  # install_no_group
        body = (
            f"{head}Thanks for adding DropTracker to {server}! One step left: create your group "
            f"so drops start posting.\n\n{SITE}/groups/new (or run /group-setup in your server)."
        )
    footer = "" if reminder else "\n\nThis is the only reminder we'll send about it, apart from one follow-up."
    return (body + footer)[:1900]


# --- presence history (called from cache_bot_guilds) ---------------------------------


def record_bot_presence(cur_ids: set, now: Optional[datetime] = None) -> tuple[int, int]:
    """Diff this bot:guilds refresh against the last one and write join/leave
    rows. Returns (joins, leaves) recorded. Sync: run it in a thread."""
    from db.models import GuildBotEvent, Session
    from utils.redis import redis_client

    now = now or datetime.now()
    raw = redis_client.get(PREV_IDS_KEY)
    prev = set(json.loads(raw)) if raw else None
    if not snapshot_trustworthy(prev, cur_ids):
        app_logger.log(log_type="warning", data=f"bot:guilds shrank {len(prev or [])}->{len(cur_ids)}; skipped presence diff",
                       app_name="setup_alerts", description="record_bot_presence")
        return 0, 0
    joined, left = diff_presence(prev, cur_ids)
    if joined or left:
        s = Session()
        try:
            for gid in joined:
                s.add(GuildBotEvent(guild_id=str(gid), event="join", source="sweep", at=now))
            for gid in left:
                s.add(GuildBotEvent(guild_id=str(gid), event="leave", source="sweep", at=now))
            s.commit()
        finally:
            s.close()
    redis_client.set(PREV_IDS_KEY, json.dumps(sorted(cur_ids)))
    return len(joined), len(left)


# --- detection -----------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    subject: str
    group_id: Optional[int]
    guild_id: Optional[str]
    group_name: Optional[str]
    detail: Optional[str]
    # Whether a NEW incident may open (vs baseline) when no row exists yet.
    may_open: bool


def _present_ids() -> Optional[set]:
    from utils.redis import redis_client

    raw = redis_client.get("bot:guilds")
    if not raw:
        return None
    try:
        return {str(g.get("id")) for g in json.loads(raw) if g.get("id")}
    except (TypeError, ValueError):
        return None


def _since(now: datetime) -> datetime:
    """When alerts started on this install (set on the first run)."""
    from utils.redis import redis_client

    raw = redis_client.get(SINCE_KEY)
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    redis_client.set(SINCE_KEY, now.isoformat())
    return now


def detect(s, now: datetime) -> tuple[list[Finding], set]:
    """Every condition that holds right now. Returns (findings, subjects
    checked) so incidents for checked subjects that no longer hold resolve."""
    from sqlalchemy import func, text

    from db.models import Group, GroupConfiguration, Guild, GuildBotEvent, NotifiedSubmission

    present = _present_ids()
    if present is None:
        return [], set()
    since = _since(now)
    findings: list[Finding] = []
    checked: set = set()

    groups = [
        g for g in s.query(Group.group_id, Group.group_name, Group.guild_id, Group.date_added).all()
        if g.group_id not in EXCLUDED_GROUPS
    ]
    left_after = {
        r.guild_id
        for r in s.query(GuildBotEvent.guild_id).filter(GuildBotEvent.event == "leave", GuildBotEvent.at >= since)
    }

    # bot_missing
    for g in groups:
        if not g.guild_id:
            continue
        subject = f"g{g.group_id}"
        checked.add(("bot_missing", subject))
        if str(g.guild_id) not in present:
            findings.append(Finding("bot_missing", subject, g.group_id, str(g.guild_id), g.group_name,
                                    None, may_open=str(g.guild_id) in left_after))

    # posts_failing: one bounded aggregate over the last day.
    day_ago = now - timedelta(days=1)
    # One aggregate row per group: posts that went out, channel-error
    # failures, and a sample error for the message.
    rows = s.execute(text(
        "SET STATEMENT max_statement_time=20 FOR "
        "SELECT group_id, SUM(status = 'sent') AS sent, "
        "SUM(status = 'failed' AND LOWER(COALESCE(error_message, '')) REGEXP :pattern) AS fails, "
        "SUBSTRING(MAX(CASE WHEN status = 'failed' THEN error_message END), 1, 160) AS sample "
        "FROM notification_queue "
        "WHERE created_at >= :since AND status IN ('sent', 'failed') AND group_id IS NOT NULL "
        "GROUP BY group_id"
    ), {"since": day_ago, "pattern": "|".join(CHANNEL_ERROR_MARKERS)}).fetchall()
    per = {int(gid): {"sent": int(sent or 0), "fail": int(fails or 0), "last_err": sample}
           for gid, sent, fails, sample in rows}
    names = {g.group_id: g for g in groups}
    for gid, p in per.items():
        if gid not in names:
            continue
        subject = f"g{gid}"
        checked.add(("posts_failing", subject))
        if p["fail"] >= FAILS_TO_ALERT and p["sent"] == 0:
            worked_recently = s.query(NotifiedSubmission.id).filter(
                NotifiedSubmission.group_id == gid,
                NotifiedSubmission.date_added >= now - timedelta(days=30),
            ).first() is not None
            findings.append(Finding("posts_failing", subject, gid, names[gid].guild_id, names[gid].group_name,
                                    p["last_err"], may_open=worked_recently))
    # Groups that had failures before but none today resolve on their own:
    for g in groups:
        checked.add(("posts_failing", f"g{g.group_id}"))

    # no_channel: only groups created since alerts started, 24h+ old, bot present.
    new_groups = [g for g in groups if g.date_added and since <= g.date_added <= now - STALL_AFTER]
    if new_groups:
        ids = [g.group_id for g in new_groups]
        have = {
            r.group_id
            for r in s.query(GroupConfiguration.group_id).filter(
                GroupConfiguration.group_id.in_(ids),
                GroupConfiguration.config_key == "channel_id_to_post_loot",
                func.coalesce(GroupConfiguration.config_value, "").notin_(["", "0"]),
            )
        }
        for g in new_groups:
            subject = f"g{g.group_id}"
            checked.add(("no_channel", subject))
            if g.group_id not in have and g.guild_id and str(g.guild_id) in present:
                findings.append(Finding("no_channel", subject, g.group_id, str(g.guild_id), g.group_name,
                                        None, may_open=True))
    for g in groups:
        checked.add(("no_channel", f"g{g.group_id}"))

    # install_no_group: joins seen since alerts started, 24h+ ago, still present, no group.
    joins = s.query(GuildBotEvent.guild_id, func.max(GuildBotEvent.at)).filter(
        GuildBotEvent.event == "join", GuildBotEvent.at >= since,
    ).group_by(GuildBotEvent.guild_id).all()
    linked = {str(r.guild_id) for r in s.query(Guild.guild_id).filter(Guild.group_id.isnot(None))}
    linked |= {str(g.guild_id) for g in groups if g.guild_id}
    for guild_id, joined_at in joins:
        subject = f"s{guild_id}"
        checked.add(("install_no_group", subject))
        if joined_at <= now - STALL_AFTER and guild_id in present and guild_id not in linked:
            findings.append(Finding("install_no_group", subject, None, guild_id, None, None, may_open=True))

    return findings, checked


def apply_findings(s, findings: list[Finding], checked: set, now: datetime) -> list:
    """Upsert incident rows from this run. Returns the rows that are due a
    message (open, under the cap, reminder interval passed)."""
    from db.models import GroupSetupAlert

    holding = {(f.kind, f.subject): f for f in findings}
    rows = {(r.kind, r.subject): r for r in s.query(GroupSetupAlert).all()}

    for key, f in holding.items():
        row = rows.get(key)
        if row is None:
            row = GroupSetupAlert(kind=f.kind, subject=f.subject, group_id=f.group_id, guild_id=f.guild_id,
                                  state="open" if f.may_open else "baseline", strikes=0,
                                  first_seen_at=now, last_seen_at=now, send_count=0)
            s.add(row)
            rows[key] = row
        elif row.state == "resolved":
            # A relapse is a new incident, unless it is merely the old baseline.
            row.state = "open" if f.may_open else "baseline"
            row.first_seen_at, row.send_count, row.last_sent_at = now, 0, None
            row.delivered, row.last_error, row.resolved_at, row.strikes = None, None, None, 0
        row.strikes = (row.strikes or 0) + 1
        row.last_seen_at = now
        row.detail = f.detail or row.detail
        row.group_id, row.guild_id = f.group_id or row.group_id, f.guild_id or row.guild_id

    for key, row in rows.items():
        if key in checked and key not in holding and row.state in ("open", "baseline"):
            row.state, row.resolved_at = "resolved", now

    s.commit()
    due = []
    for key, row in rows.items():
        if key not in holding:
            continue
        if row.kind == "bot_missing" and (row.strikes or 0) < BOT_MISSING_STRIKES:
            continue
        if should_send(row.kind, AlertRow(row.state, row.send_count or 0, row.last_sent_at), now):
            due.append(row)
    return due


def _recipients(s, row) -> list[tuple[str, str]]:
    """[(discord_id, role)] in the order to try. The Discord server owner is
    filled in later by the async sender (needs the bot)."""
    from db.models import GroupAdmin, User

    if not row.group_id:
        return []
    out: list[tuple[str, str]] = []
    admins = (
        s.query(GroupAdmin.role, User.discord_id)
        .join(User, User.user_id == GroupAdmin.user_id)
        .filter(GroupAdmin.group_id == row.group_id)
        .all()
    )
    for role, did in sorted(admins, key=lambda a: 0 if a.role == "owner" else 1):
        if did:
            out.append((str(did), "owner" if role == "owner" else "admin"))
    return out


async def _guild_info(bot, guild_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(server owner's discord id, server name), best effort."""
    if not guild_id:
        return None, None
    try:
        guild = await bot.fetch_guild(int(guild_id))
    except Exception:  # noqa: BLE001
        return None, None
    if guild is None:
        return None, None
    owner = getattr(guild, "_owner_id", None) or getattr(guild, "owner_id", None)
    return (str(owner) if owner else None), getattr(guild, "name", None)


async def _try_dm(bot, discord_id: str, content: str) -> Optional[str]:
    """None on success, else the reason it failed."""
    try:
        user = await bot.fetch_user(int(discord_id))
    except Exception as e:  # noqa: BLE001
        return f"fetch_user: {e}"[:200]
    if user is None:
        return "user not reachable"
    try:
        await user.send(content)
        return None
    except Exception as e:  # noqa: BLE001
        return f"send: {e}"[:200]


async def run_setup_alerts(bot) -> dict:
    """One detection + delivery pass. Safe to call repeatedly."""
    from db.models import GroupSetupAlert, Session

    current_mode = mode()
    if current_mode == "off":
        return {"mode": "off"}
    now = datetime.now()

    def _detect():
        s = Session()
        try:
            findings, checked = detect(s, now)
            due = apply_findings(s, findings, checked, now)
            plans = []
            for row in due:
                plans.append({
                    "id": row.id, "kind": row.kind, "group_id": row.group_id, "guild_id": row.guild_id,
                    "group_name": _group_name(s, row.group_id), "detail": row.detail,
                    "reminder": (row.send_count or 0) > 0, "recipients": _recipients(s, row),
                })
            return len(findings), plans
        finally:
            s.close()

    found, plans = await asyncio.to_thread(_detect)
    results = []
    for plan in plans:
        guild_owner, guild_name = await _guild_info(bot, plan["guild_id"])
        owner_first = [r for r in plan["recipients"] if r[1] == "owner"]
        admins = [r for r in plan["recipients"] if r[1] == "admin"]
        order = recipient_order(
            owner_first[0][0] if owner_first else None, guild_owner, [a[0] for a in admins]
        )
        roles = {d: ("owner" if owner_first and d == owner_first[0][0] else
                     "guild_owner" if d == guild_owner else "admin") for d in order}
        content = build_message(plan["kind"], group_id=plan["group_id"], group_name=plan["group_name"],
                                guild_id=plan["guild_id"], guild_name=guild_name, detail=plan["detail"],
                                reminder=plan["reminder"])
        delivered_to, error = None, "no reachable owner or admin"
        for did in order:
            if current_mode == "log":
                delivered_to, error = did, "log mode: not sent"
                break
            err = await _try_dm(bot, did, content)
            if err is None:
                delivered_to, error = did, None
                break
            error = err
        results.append((plan["id"], delivered_to, roles.get(delivered_to), error, content))
        app_logger.log(log_type="info", data=json.dumps({
            "kind": plan["kind"], "group_id": plan["group_id"], "guild_id": plan["guild_id"],
            "mode": current_mode, "to": delivered_to, "role": roles.get(delivered_to), "error": error,
        }), app_name="setup_alerts", description="setup alert")
        await asyncio.sleep(1)  # gentle on DM rate limits

    def _record():
        s = Session()
        try:
            for alert_id, to, role, error, _content in results:
                row = s.query(GroupSetupAlert).filter(GroupSetupAlert.id == alert_id).first()
                if row is None:
                    continue
                row.send_count = (row.send_count or 0) + 1
                row.last_sent_at = now
                row.recipient_discord_id, row.recipient_role = to, role
                row.delivered = error is None if current_mode == "send" else None
                row.last_error = error
            s.commit()
        finally:
            s.close()

    if results:
        await asyncio.to_thread(_record)
    return {"mode": current_mode, "conditions": found, "messaged": len(results)}


def _group_name(s, group_id: Optional[int]) -> Optional[str]:
    if not group_id:
        return None
    from db.models import Group

    g = s.query(Group.group_name).filter(Group.group_id == group_id).first()
    return g.group_name if g else None
