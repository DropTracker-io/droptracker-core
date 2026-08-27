"""Shapes the bot's guild channel cache (``guild:{id}:channels``).

The cache carries several entry kinds so the website's channel pickers can
offer forum threads as notification destinations (suggestion #3 — groups that
keep e.g. one "achievements" forum with a thread per notification type instead
of a dozen text channels):

    {"id", "name", "position", "type": "text"}
    {"id", "name", "position", "type": "forum"}
    {"id", "name", "position", "type": "category"}
    {"id", "name", "position", "type": "voice"}
    {"id", "name", "position", "type": "thread", "parent_id": "<forum/text id>"}

Note this cache is NOT a list of places the bot can post. It is the guild's
channel inventory, and each picker filters it for its own purpose — voice
channels are here for the `vc_to_display_*` stat displays, which rename a
channel rather than write to it, and categories for per-team channel parents.
A picker choosing a notification destination must select for messageable kinds
itself rather than assume everything listed qualifies.

Threads are emitted immediately after their parent channel, so consumers that
ignore ``type`` still see a sensibly ordered flat list. Only *active* threads
are listed (one ``GET /guilds/{id}/threads/active`` call — archived threads
would need a paginated fetch per channel, and pasting an archived thread's id
still works: Discord auto-unarchives on message send).

Pure shaping lives here (unit-testable without a bot); the fetching stays in
``bots/main.py``.
"""
from __future__ import annotations

from typing import Iterable, List


def shape_channel_cache(raw_channels: Iterable, threads: Iterable) -> List[dict]:
    """Build the cache payload from fetched guild channels + active threads.

    ``raw_channels`` are interactions.py channel objects (or anything with
    ``id``/``name``/``position`` and a class name); ``threads`` need
    ``id``/``name``/``parent_id``. Threads whose parent isn't a cached
    text/forum channel (e.g. under an announcement channel) are dropped.
    """
    channels = []
    for c in raw_channels:
        kind = type(c).__name__
        if kind == "GuildText":
            ctype = "text"
        elif kind == "GuildForum":
            ctype = "forum"
        elif kind == "GuildCategory":
            # Surfaced so the website can offer a category picker for per-team
            # channels (private, unlike forum threads). Categories aren't
            # messageable, so notification-destination pickers must skip them.
            ctype = "category"
        elif kind in ("GuildVoice", "GuildStageVoice"):
            # For the `vc_to_display_monthly_loot` / `..._droptracker_users`
            # stat displays, which RENAME the channel every 10 minutes rather
            # than post in it — so "not messageable" is no reason to withhold
            # them. Group admins previously had to paste a raw channel id
            # because these never reached the picker at all.
            ctype = "voice"
        else:
            continue
        channels.append(
            {"id": str(c.id), "name": c.name, "position": c.position or 0, "type": ctype}
        )
    channels.sort(key=lambda c: c["position"])

    threads_by_parent: dict[str, list[dict]] = {}
    for t in threads:
        parent_id = str(getattr(t, "parent_id", "") or "")
        if not parent_id:
            continue
        threads_by_parent.setdefault(parent_id, []).append(
            {"id": str(t.id), "name": t.name, "type": "thread", "parent_id": parent_id}
        )

    out: List[dict] = []
    for channel in channels:
        out.append(channel)
        for thread in sorted(threads_by_parent.get(channel["id"], []), key=lambda t: t["name"].lower()):
            thread["position"] = channel["position"]
            out.append(thread)
    return out
