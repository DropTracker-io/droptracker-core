"""Repair channel config values that aren't channel ids (web138a).

Before web138a, channel keys accepted any string, so some groups stored a
channel NAME ("drop-tracker") or a pasted discord.com link, and every
notification to them failed. For each stored channel-key value that isn't a
plain id:

  - a channel/message link or <#mention> becomes the id it contains;
  - a name is matched against the bot's cached channel list for the group's
    server (Redis ``guild:{id}:channels``, text/forum/thread only) when exactly
    one channel has that name;
  - anything else is cleared, so the group shows "no channel" in its setup
    checklist instead of failing silently.

Dry run by default; ``--apply`` writes. Idempotent.

    venv/bin/python -m scripts.clean_channel_configs [--apply] [--group 288]
"""
from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from db.models import Group, GroupConfiguration, session  # noqa: E402
from utils.redis import redis_client  # noqa: E402
from web_api.config_registry import GROUP_CONFIG_FIELDS, SEASONAL_PREFIX, parse_channel_ref  # noqa: E402

CHANNEL_KEYS = {f["key"] for f in GROUP_CONFIG_FIELDS if f["type"] == "channel"}
CHANNEL_KEYS |= {SEASONAL_PREFIX + k for k in CHANNEL_KEYS}


def _name_index(guild_id: str) -> dict[str, list[str]]:
    raw = redis_client.get(f"guild:{guild_id}:channels")
    if not raw:
        return {}
    out: dict[str, list[str]] = {}
    for ch in json.loads(raw):
        if ch.get("type") in ("text", "forum", "thread"):
            out.setdefault(str(ch.get("name", "")).strip().lower(), []).append(str(ch["id"]))
    return out


def plan(group_filter: int | None = None) -> list[tuple]:
    q = session.query(GroupConfiguration).filter(GroupConfiguration.config_key.in_(CHANNEL_KEYS))
    if group_filter:
        q = q.filter(GroupConfiguration.group_id == group_filter)
    guilds = {g.group_id: g.guild_id for g in session.query(Group.group_id, Group.guild_id)}
    indexes: dict[str, dict] = {}
    changes = []
    for row in q:
        value = (row.config_value or "").strip()
        if value in ("", "0"):
            continue  # unset; "0" is the legacy unset sentinel, readers handle it
        parsed = parse_channel_ref(value)
        if parsed == value:
            continue  # already an id
        if parsed is not None:
            changes.append((row, value, parsed, "parsed link/mention"))
            continue
        guild_id = guilds.get(row.group_id)
        name = value.lstrip("#").strip().lower()
        match = None
        if guild_id:
            idx = indexes.setdefault(str(guild_id), _name_index(str(guild_id)))
            hits = idx.get(name, [])
            match = hits[0] if len(hits) == 1 else None
        changes.append((row, value, match or "", f"matched name '{name}'" if match else "cleared (not a channel)"))
    return changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--group", type=int)
    args = ap.parse_args(argv)
    changes = plan(args.group)
    for row, old, new, why in changes:
        print(f"group {row.group_id:>6} {row.config_key:<32} {old[:60]!r:<64} -> {new or '(empty)'}  [{why}]")
    print(f"{len(changes)} value(s) to change{'' if args.apply else ' (dry run; --apply to write)'}")
    if args.apply and changes:
        for row, _old, new, _why in changes:
            row.config_value = new
        session.commit()
        from utils import group_config

        for gid in {row.group_id for row, *_ in changes}:
            try:
                group_config.invalidate(gid)
            except Exception:  # noqa: BLE001
                pass
        print("applied")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
