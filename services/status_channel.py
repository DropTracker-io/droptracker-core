"""#status channel maintainer — two persistent Components-V2 cards, edited in
place by the core bot (post once, edit forever; repost only if deleted):

  1. Service status — intake API + legacy webhook reader health, live counters
     from services/status_metrics.py. Refreshed every sweep (1 min).
  2. Known issues — curated in /admin/status (web CP). Re-rendered when the
     web API bumps status:issues:rev, plus a periodic safety refresh.

The sweep never raises (a broken status card must not hurt the core bot), and
the cards carry an "updated <t:..:R>" stamp so a frozen panel is self-evident
if this bot itself dies — external paging stays scripts/health_watch.py's job.

Message ids persist in the Redis hash status:channel:messages so restarts
edit the same messages instead of reposting.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from db.app_logger import AppLogger

app_logger = AppLogger()

STATUS_CHANNEL_ID = int(os.getenv("DISCORD_STATUS_CHANNEL_ID", "1533817970311172116"))

MESSAGES_KEY = "status:channel:messages"

# Re-render the issues card at least every N sweeps even without a rev bump
# (covers manual DB edits and keeps its "updated" stamp honest).
ISSUES_FORCE_EVERY = 15

_ACCENT = {
    "operational": 0x2ECC71,
    "degraded": 0xE67E22,
    "offline": 0xE74C3C,
}

_SERVICE_STATE = {
    "operational": ("🟢", "Operational"),
    "degraded": ("🟠", "Degraded"),
    "offline": ("🔴", "Offline"),
}

SEVERITY_META = {
    "major": ("🔴", "Major"),
    "degraded": ("🟠", "Degraded"),
    "minor": ("🟡", "Minor"),
    "info": ("🔵", "Info"),
}

# Rough safety margin under Discord's 4000-char budget for V2 text displays.
_ISSUES_CHAR_BUDGET = 3400

_last_rendered_rev: Optional[int] = None
_sweeps_since_issues = 0

# Overlap guard: when Discord REST stalls, 1-min sweeps pile up; without this,
# every stalled sweep resumed at once and each posted its own card copy.
_sweep_lock = asyncio.Lock()


def _fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _worst_status(snapshot: dict) -> str:
    ranks = {"operational": 0, "degraded": 1, "offline": 2}
    worst = "operational"
    for svc in ("api", "webhook"):
        s = (snapshot.get(svc) or {}).get("status", "offline")
        if ranks.get(s, 2) > ranks[worst]:
            worst = s
    return worst


def _service_block(title: str, data: dict, *, extra: Optional[str] = None) -> str:
    emoji, label = _SERVICE_STATE.get(data.get("status", "offline"), ("🔴", "Offline"))
    counts = data.get("processed") or {}
    lines = [
        f"{emoji} **{title}** — {label}",
        f"👥 **{_fmt(data.get('players_1h'))}** players active (last hour)",
        "📦 Processed **{}** (5m) · **{}** (30m) · **{}** (24h)".format(
            _fmt(counts.get("5m")), _fmt(counts.get("30m")), _fmt(counts.get("24h"))
        ),
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def build_services_components(snapshot: dict) -> list:
    """Pure renderer: snapshot dict (collect_service_snapshot) -> V2 components."""
    from interactions.models import ContainerComponent, SeparatorComponent, TextDisplayComponent

    api = snapshot.get("api") or {}
    webhook = snapshot.get("webhook") or {}
    updated = int(snapshot.get("generated_at") or time.time())

    api_extra = None
    if api.get("status") == "degraded":
        depth = api.get("queue_depth")
        if not api.get("consumer_alive"):
            api_extra = "⚠️ Queue consumer is not responding — submissions are accepted but processing is paused."
        elif depth:
            api_extra = f"⚠️ Processing backlog: **{_fmt(depth)}** submissions queued."
    elif api.get("status") == "offline":
        api_extra = "⚠️ The intake API is not responding — plugin submissions are failing."

    webhook_extra = None
    if webhook.get("status") == "offline":
        webhook_extra = "⚠️ The webhook reader is offline — Discord-webhook submissions are not being processed."

    children = [
        TextDisplayComponent(content="## 🛰️ DropTracker Service Status"),
        TextDisplayComponent(
            content=f"-# Live health of the submission pipeline · updated <t:{updated}:R> · refreshes every minute"
        ),
        SeparatorComponent(divider=True),
        TextDisplayComponent(content=_service_block("Submission API", api, extra=api_extra)),
        SeparatorComponent(divider=True),
        TextDisplayComponent(content=_service_block("Webhook Processing", webhook, extra=webhook_extra)),
    ]
    return [ContainerComponent(*children, accent_color=_ACCENT[_worst_status(snapshot)])]


def _issue_line(issue: dict) -> str:
    emoji, _ = SEVERITY_META.get(issue.get("severity", "minor"), ("🟡", "Minor"))
    line = f"{emoji} **{issue.get('title', '').strip()}**"
    if issue.get("status") == "monitoring":
        line += " _(monitoring)_"
    desc = (issue.get("description") or "").strip()
    if desc:
        if len(desc) > 240:
            desc = desc[:237] + "…"
        line += f"\n-# {desc}"
    created = issue.get("created_ts")
    if created:
        line += f"\n-# Known since <t:{int(created)}:d>"
    return line


def build_issues_components(categories: list, *, updated_ts: Optional[int] = None) -> list:
    """Pure renderer: category dicts (open issues only) -> V2 components."""
    from interactions.models import ContainerComponent, SeparatorComponent, TextDisplayComponent

    updated = int(updated_ts or time.time())
    children = [
        TextDisplayComponent(content="## 🧭 Known Issues"),
        TextDisplayComponent(
            content="-# Problems we are already aware of and working on — no need to report these."
        ),
    ]

    total_issues = sum(len(c.get("issues") or []) for c in categories)
    if total_issues == 0:
        children.append(SeparatorComponent(divider=True))
        children.append(TextDisplayComponent(
            content="✅ **No known issues right now.** Everything we track is behaving."
        ))
    else:
        used = 0
        shown = 0
        truncated = False
        for cat in categories:
            issues = cat.get("issues") or []
            if not issues:
                continue
            emoji = (cat.get("emoji") or "").strip()
            header = f"### {emoji + ' ' if emoji else ''}{cat.get('name', 'Other')}"
            body_lines = []
            for issue in issues:
                line = _issue_line(issue)
                if used + len(line) > _ISSUES_CHAR_BUDGET:
                    truncated = True
                    break
                body_lines.append(line)
                used += len(line)
                shown += 1
            if body_lines:
                children.append(SeparatorComponent(divider=True))
                children.append(TextDisplayComponent(content=header + "\n" + "\n".join(body_lines)))
            if truncated:
                break
        if truncated and total_issues > shown:
            children.append(TextDisplayComponent(
                content=f"-# …and **{total_issues - shown}** more — see the website status page."
            ))

    children.append(SeparatorComponent(divider=True))
    children.append(TextDisplayComponent(
        content=f"-# Curated by the DropTracker team · updated <t:{updated}:R>"
    ))
    accent = 0x2ECC71 if total_issues == 0 else 0xE67E22
    return [ContainerComponent(*children, accent_color=accent)]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_open_issues() -> list:
    """Blocking DB read (call via to_thread): categories with unresolved issues."""
    from db.models import KnownIssueCategory, Session

    session = Session()
    try:
        cats = (
            session.query(KnownIssueCategory)
            .order_by(KnownIssueCategory.order, KnownIssueCategory.id)
            .all()
        )
        out = []
        for c in cats:
            issues = []
            for i in c.issues:
                if i.status == "resolved":
                    continue
                issues.append({
                    "title": i.title,
                    "description": i.description,
                    "severity": i.severity,
                    "status": i.status,
                    "created_ts": int(i.created_at.timestamp()) if i.created_at else None,
                })
            if issues:
                out.append({"name": c.name, "emoji": c.emoji, "issues": issues})
        return out
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Message upkeep
# ---------------------------------------------------------------------------

def _redis():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def _get_stored_message_id(slot: str) -> Optional[int]:
    conn = _redis()
    if conn is None:
        return None
    try:
        raw = conn.hget(MESSAGES_KEY, slot)
        return int(raw) if raw else None
    except Exception:
        return None


def _store_message_id(slot: str, message_id) -> None:
    conn = _redis()
    if conn is None:
        return
    try:
        conn.hset(MESSAGES_KEY, slot, str(message_id))
    except Exception:
        pass


async def _upsert_card(channel, slot: str, components) -> None:
    """Edit the stored message for this slot, or post a fresh one.

    fetch_message returns None only on a genuine 404 (message deleted) — that
    is the ONLY case that may repost. Transient REST failures raise and must
    propagate to the sweep's outer catch: swallowing them here read as
    "deleted" and reposted a duplicate card per in-flight sweep when the
    Discord connection dropped (3 dupes on 2026-08-20).
    """
    message = None
    stored = _get_stored_message_id(slot)
    if stored:
        message = await channel.fetch_message(message_id=stored)
    if message is not None:
        await message.edit(components=components)
    else:
        message = await channel.send(components=components)
        _store_message_id(slot, message.id)


async def run_status_sweep(bot) -> bool:
    """One upkeep pass. Never raises; returns True when a Discord write happened."""
    if not STATUS_CHANNEL_ID:
        return False
    if _sweep_lock.locked():
        return False  # previous sweep still in flight — skip, never stack
    async with _sweep_lock:
        return await _run_status_sweep_locked(bot)


async def _run_status_sweep_locked(bot) -> bool:
    global _last_rendered_rev, _sweeps_since_issues

    try:
        if not getattr(bot, "is_ready", False):
            return False
        channel = await bot.fetch_channel(STATUS_CHANNEL_ID)
        if channel is None:
            return False

        from services.status_metrics import collect_service_snapshot, get_issues_rev

        snapshot = await asyncio.to_thread(collect_service_snapshot)
        await _upsert_card(channel, "services", build_services_components(snapshot))

        rev = await asyncio.to_thread(get_issues_rev)
        if (
            _last_rendered_rev is None
            or rev != _last_rendered_rev
            or _sweeps_since_issues >= ISSUES_FORCE_EVERY
        ):
            categories = await asyncio.to_thread(_load_open_issues)
            await _upsert_card(channel, "issues", build_issues_components(categories))
            _last_rendered_rev = rev
            _sweeps_since_issues = 0
        else:
            _sweeps_since_issues += 1
        return True
    except Exception as e:
        try:
            app_logger.log(
                log_type="error",
                data=f"status channel sweep failed: {e}",
                app_name="core",
                description="status_channel",
            )
        except Exception:
            print(f"Status channel sweep failed: {e}")
        return False
