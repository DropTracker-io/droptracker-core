#!/usr/bin/env python3
"""
Turn a restored production dump into the dev instance, repeatably
==================================================================

The dev instance runs on a copy of production. Everything that makes it the
*testing* environment rather than a stale copy lives here, so restoring a new
dump never silently undoes it:

  check      read-only: is this box set up as the dev instance? Prints facts
             and flags, never a secret.
  id-offset  move the users/players/groups id counters to 10,000,000, so rows
             created on dev never share an id with rows production creates
             later (the tester roster push relies on that).
  layout     the dev guild's BUG TESTING category, its channels and the
             Bug Tester role. Writes the ids to data/dev/ (untracked).
  groups     the reserved Bug Testers (10000001) and Mirror firehose
             (10000002) groups, wired to those channels; detaches any other
             group from the dev guild so testers are announced once.
  all        id-offset, layout, groups: run this after every restore.

Every step is a dry run unless ``--apply`` is given, and nothing is written
unless STATE=dev and DEV_ALLOWED_GUILDS includes the dev guild.

Environment the dev instance needs (see ``check``):

    STATE=dev
    DEV_ALLOWED_GUILDS=["1436315863434133598"]
    BADGE_GROUPS={"10000001":"bug_tester_helper"}
    MIRROR_SINK_GROUP_ID=10000002
    DEV_SYNC_KEY=<same Fernet key as production>
    PRIMARY_GUILD_ID=1436315863434133598
    DISCORD_ROLE_MAP_PATH=data/dev/discord_roles.json
    USER_UPLOAD_BASE_URL=https://dev-api.droptracker.io/img/user-upload/

Usage:
    python -m scripts.dev_instance check
    python -m scripts.dev_instance all --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

DEV_GUILD_ID = 1436315863434133598
#: The production relay guild the dev core app is still a member of.
RELAY_GUILD_ID = 597397938989432842
BUG_TESTERS_GROUP_ID = 10_000_001
FIREHOSE_GROUP_ID = 10_000_002
ID_OFFSET = 10_000_000
OFFSET_TABLES = ("users", "players", "groups")
BADGE_KEY = "bug_tester_helper"

LAYOUT_PATH = PROJECT_ROOT / "data" / "dev" / "dev_guild_layout.json"
DEV_ROLE_MAP_PATH = PROJECT_ROOT / "data" / "dev" / "discord_roles.json"

#: [DEV] DropTracker (Core) and (Webhooks). They must see the category despite
#: its @everyone deny; only those actually in the guild are given overwrites.
KNOWN_DEV_BOT_IDS = ("1143656105352896513", "1088910298305548409")
STAFF_ROLE_NAMES = ("Owner", "Staff", "Developer")
ROLE_NAME = "Bug Tester"
ROLE_COLOR = 0x2ECC71
CATEGORY_NAME = "BUG TESTING"
REASON = "DropTracker dev instance setup"

API = "https://discord.com/api/v10"
TEXT, CATEGORY, FORUM = 0, 4, 15

# Discord permission bits.
ADD_REACTIONS = 1 << 6
VIEW = 1 << 10
SEND = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED = 1 << 14
ATTACH = 1 << 15
HISTORY = 1 << 16
MANAGE_THREADS = 1 << 34
PUBLIC_THREADS = 1 << 35
THREAD_SEND = 1 << 38

TESTER_TALK = VIEW | SEND | HISTORY | ATTACH | EMBED | ADD_REACTIONS | THREAD_SEND | PUBLIC_THREADS
READ_ONLY = VIEW | HISTORY | ADD_REACTIONS
STAFF = VIEW | SEND | HISTORY | ATTACH | EMBED | ADD_REACTIONS | THREAD_SEND | PUBLIC_THREADS \
    | MANAGE_MESSAGES | MANAGE_THREADS
BOT = VIEW | SEND | HISTORY | ATTACH | EMBED | ADD_REACTIONS | THREAD_SEND | MANAGE_MESSAGES \
    | MANAGE_THREADS

#: (key, name, type, access, topic). Access: open_read | testers | testers_read | staff.
CHANNELS = (
    ("start_here", "start-here", TEXT, "open_read",
     "How bug testing works here, and how to report what you find."),
    ("bug_reports", "bug-reports", FORUM, "testers",
     "One post per problem: what you did, what you expected, what happened."),
    ("tester_chat", "tester-chat", TEXT, "testers",
     "Talk to the other testers and to us."),
    ("tester_drops", "tester-drops", TEXT, "testers_read",
     "Your submissions, as the dev build announces them."),
    ("tester_lootboard", "tester-lootboard", TEXT, "testers_read",
     "The Bug Testers group's lootboard, drawn by the dev build."),
    ("tester_events", "tester-events", TEXT, "testers",
     "Events run on the dev build, for testers."),
    ("dev_changelog", "dev-changelog", TEXT, "testers_read",
     "What is deployed on the dev instance right now."),
    ("mirror_firehose", "mirror-firehose", TEXT, "staff",
     "Everyone-mode mirror output. Staff only."),
)
FORUM_TAGS = ("plugin", "website", "discord bot", "events", "fixed")

START_HERE_MESSAGE = (
    "## Bug testing happens here\n"
    "-# The dev build runs ahead of the live site. Things will break; that is the point.\n\n"
    "**Play as normal.** While you have the Bug Tester role, the dev build gets a copy of "
    "everything you submit. Your real tracking is not affected.\n\n"
    "**Watch** {tester_drops} to see how the dev build announces your drops, PBs and "
    "collection log slots, and {tester_events} for test events.\n\n"
    "**Report** anything that looks wrong in {bug_reports}: one post per problem, with what "
    "you did, what you expected and what happened. Screenshots help.\n\n"
    "**What's new** on the dev build is posted in {dev_changelog}.\n\n"
    "-# The Bug Tester role follows the badge on the main DropTracker server. If it "
    "disappears here, it was removed there."
)

#: Group config written on top of the template group's rows. Values are stored
#: text: booleans "1"/"0", numbers as digits, channels by layout key.
_ANNOUNCE_ALL = {k: "1" for k in (
    "notify_clogs", "notify_cas", "notify_pets", "notify_quests", "notify_diaries",
    "notify_deaths", "notify_levels", "notify_pbs", "notify_kc_milestones",
)}
_ANNOUNCE_DROPS_ONLY = {k: "0" for k in (
    "notify_clogs", "notify_cas", "notify_quests", "notify_special_quests", "notify_diaries",
    "notify_deaths", "notify_levels", "notify_pbs", "notify_kc_milestones",
    "notify_rank_milestones", "notify_points_awarded",
)}
GROUP_SPECS = (
    {
        "group_id": BUG_TESTERS_GROUP_ID,
        "name": "Bug Testers",
        "description": "DropTracker's bug testers. Members follow the Bug Tester badge.",
        "maps_guild": True,
        "channels": {
            "channel_id_to_post_loot": "tester_drops",
            "channel_id_to_post_pets": "tester_drops",
            "lootboard_channel_id": "tester_lootboard",
        },
        "values": {
            "minimum_value_to_notify": "1",
            "only_send_messages_with_images": "0",
            "send_stacks_of_items": "1",
            **_ANNOUNCE_ALL,
        },
    },
    {
        "group_id": FIREHOSE_GROUP_ID,
        "name": "Mirror firehose",
        "description": "Everyone-mode mirrored submissions are rerouted here.",
        "maps_guild": False,
        "channels": {
            "channel_id_to_post_loot": "mirror_firehose",
            "channel_id_to_post_pets": "mirror_firehose",
            "lootboard_channel_id": None,
        },
        "values": {
            "minimum_value_to_notify": "5000000",
            "only_send_messages_with_images": "0",
            "send_stacks_of_items": "0",
            "notify_pets": "1",
            **_ANNOUNCE_DROPS_ONLY,
        },
    },
)


# --------------------------------------------------------------------------- #
# Guards and small helpers
# --------------------------------------------------------------------------- #
def _flag(name: str) -> str:
    return (os.getenv(name) or "").strip().strip('"').strip("'")


def _is_dev() -> bool:
    from utils.dev_guild_guard import is_dev_mode

    return is_dev_mode()


def require_dev() -> None:
    from utils.dev_guild_guard import allowed_guild_ids

    if not _is_dev():
        sys.exit("refusing: STATE/STATUS is not 'dev' — this is not the dev instance")
    if DEV_GUILD_ID not in allowed_guild_ids():
        sys.exit(f"refusing: DEV_ALLOWED_GUILDS does not include the dev guild {DEV_GUILD_ID}")


def _say(apply: bool, text: str) -> None:
    print(("  " if apply else "  [dry run] ") + text)


class DiscordError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Discord answered {status}: {body}")
        self.status = status


class Discord:
    def __init__(self, token: str):
        import requests

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}",
            "User-Agent": "DropTracker (https://www.droptracker.io, dev-instance setup)",
        })

    def call(self, method: str, path: str, payload=None):
        headers = {"X-Audit-Log-Reason": REASON} if method != "GET" else None
        for _ in range(6):
            response = self.session.request(method, API + path, json=payload,
                                            headers=headers, timeout=20)
            if response.status_code == 429:
                try:
                    wait = float(response.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(wait + 0.25)
                continue
            if response.status_code >= 400:
                raise DiscordError(response.status_code, response.text[:300])
            return response.json() if response.content else None
        raise DiscordError(429, "still rate limited after retries")


def _overwrite(target_id, kind: int, allow: int = 0, deny: int = 0) -> dict:
    return {"id": str(target_id), "type": kind, "allow": str(allow), "deny": str(deny)}


def _overwrite_set(overwrites) -> set:
    return {(str(o["id"]), int(o["type"]), str(o["allow"]), str(o["deny"])) for o in overwrites or ()}


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def step_check() -> int:
    from services.tester_roster import sync_key
    from utils.dev_guild_guard import allowed_guild_ids, allowed_user_ids
    from db.badge_groups import configured_badge_groups

    problems = 0

    def line(ok: bool, text: str, warn_only: bool = False):
        nonlocal problems
        mark = "ok  " if ok else ("warn" if warn_only else "FIX ")
        if not ok and not warn_only:
            problems += 1
        print(f"  [{mark}] {text}")

    print("environment")
    line(_is_dev(), "STATE/STATUS is 'dev'")
    guilds = allowed_guild_ids()
    line(DEV_GUILD_ID in guilds, f"DEV_ALLOWED_GUILDS includes the dev guild ({sorted(guilds)})")
    line(RELAY_GUILD_ID not in guilds,
         f"DEV_ALLOWED_GUILDS no longer includes production relay guild {RELAY_GUILD_ID}", warn_only=True)
    line(bool(allowed_user_ids()), "DEV_ALLOWED_USERS is set (your own test DMs)", warn_only=True)
    line(configured_badge_groups() == {BUG_TESTERS_GROUP_ID: BADGE_KEY},
         f'BADGE_GROUPS is {{"{BUG_TESTERS_GROUP_ID}":"{BADGE_KEY}"}}')
    line(_flag("MIRROR_SINK_GROUP_ID") == str(FIREHOSE_GROUP_ID), f"MIRROR_SINK_GROUP_ID is {FIREHOSE_GROUP_ID}")
    key = sync_key()
    key_ok = False
    if key:
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode("ascii"))
            key_ok = True
        except Exception:
            key_ok = False
    line(key_ok, "DEV_SYNC_KEY is set and is a valid Fernet key")
    line(_flag("PRIMARY_GUILD_ID") == str(DEV_GUILD_ID), f"PRIMARY_GUILD_ID is the dev guild")
    role_map_env = _flag("DISCORD_ROLE_MAP_PATH")
    line(bool(role_map_env), "DISCORD_ROLE_MAP_PATH is set (Bug Tester role sync on the dev guild)")
    base = _flag("USER_UPLOAD_BASE_URL")
    line(bool(base) and "www.droptracker.io" not in base,
         "USER_UPLOAD_BASE_URL points at this instance (testers' screenshots)")
    try:
        from utils.image_storage import offload_enabled

        line(not offload_enabled() or _flag("DEV_ALLOW_B2").lower() in ("1", "true", "yes", "on"),
             "screenshots are stored locally, not in the production bucket")
    except Exception as exc:
        line(False, f"could not evaluate image storage: {exc}", warn_only=True)
    line(_flag("WEBHOOK_QUEUE_MODE").lower() in ("1", "true", "yes"),
         "WEBHOOK_QUEUE_MODE is on (the firehose is only processed through the queue)", warn_only=True)
    line(bool(_flag("DEV_TOKEN")), "DEV_TOKEN is set")
    line(not _flag("BOT_TOKEN"), "BOT_TOKEN is empty, so a wrong STATE cannot connect as production")

    print("files")
    line(LAYOUT_PATH.exists(), f"{LAYOUT_PATH.relative_to(PROJECT_ROOT)} exists (run: layout --apply)")
    if DEV_ROLE_MAP_PATH.exists():
        try:
            data = json.loads(DEV_ROLE_MAP_PATH.read_text())
            line(str(data.get("guild_id")) == str(DEV_GUILD_ID) and bool((data.get("roles") or {}).get("bug_tester")),
                 "the dev role map names the Bug Tester role in the dev guild")
        except ValueError:
            line(False, "the dev role map is not valid JSON")
    else:
        line(False, f"{DEV_ROLE_MAP_PATH.relative_to(PROJECT_ROOT)} exists (run: layout --apply)")

    print("database")
    try:
        from sqlalchemy import text

        from db.models import Group, Guild, Session

        with Session() as s:
            for spec in GROUP_SPECS:
                group = s.get(Group, spec["group_id"])
                line(group is not None and str(group.guild_id) == str(DEV_GUILD_ID),
                     f"group {spec['group_id']} ({spec['name']}) exists in the dev guild")
            guild = s.get(Guild, str(DEV_GUILD_ID))
            line(guild is not None and guild.group_id == BUG_TESTERS_GROUP_ID,
                 "the dev guild maps to the Bug Testers group")
            others = [gid for (gid,) in s.query(Group.group_id)
                      .filter(Group.guild_id == str(DEV_GUILD_ID),
                              Group.group_id.notin_([s_["group_id"] for s_ in GROUP_SPECS])).all()]
            line(not others, f"no other group is attached to the dev guild ({others or 'none'})")
            members = s.execute(text(
                "SELECT COUNT(*) FROM user_group_association WHERE group_id = :g AND player_id IS NOT NULL"
            ), {"g": BUG_TESTERS_GROUP_ID}).scalar()
            print(f"  [info] Bug Testers group has {members} member accounts")
            for table in OFFSET_TABLES:
                value = _auto_increment(s, table)
                line(value is not None and value >= ID_OFFSET, f"{table} ids continue from {value}")
            s.rollback()
    except Exception as exc:
        line(False, f"database check failed: {exc}")

    print("tester roster")
    try:
        from utils.redis import RedisClient
        from services.tester_roster import DEV_DISCORD_IDS_KEY, DEV_LAST_TS_KEY

        client = RedisClient().client
        last = client.get(DEV_LAST_TS_KEY)
        count = client.scard(DEV_DISCORD_IDS_KEY)
        if last:
            age = time.time() - float(last)
            line(age < 900, f"last roster from production arrived {int(age)}s ago; {count} testers",
                 warn_only=True)
        else:
            line(False, "no roster has arrived from production yet", warn_only=True)
    except Exception as exc:
        line(False, f"could not read roster state: {exc}", warn_only=True)

    print(f"\n{problems} thing(s) to fix" if problems else "\nall required checks pass")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# id-offset
# --------------------------------------------------------------------------- #
def _auto_increment(session, table: str):
    from sqlalchemy import text

    return session.execute(text(
        "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table}).scalar()


def step_id_offset(apply: bool) -> int:
    from sqlalchemy import text

    from db.models import Session

    print(f"id offset (tables: {', '.join(OFFSET_TABLES)})")
    with Session() as s:
        for table in OFFSET_TABLES:
            current = _auto_increment(s, table)
            if current is None:
                print(f"  {table}: not found")
                continue
            if current >= ID_OFFSET:
                print(f"  {table}: already at {current}")
                continue
            _say(apply, f"{table}: {current} -> {ID_OFFSET}")
            if apply:
                s.execute(text(f"ALTER TABLE `{table}` AUTO_INCREMENT = {ID_OFFSET}"))
        s.commit()
    return 0


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def _channel_overwrites(access: str, everyone: str, tester_role: str, staff_roles, bots) -> list:
    out = []
    if access == "open_read":
        out.append(_overwrite(everyone, 0, allow=READ_ONLY, deny=SEND | PUBLIC_THREADS))
    else:
        out.append(_overwrite(everyone, 0, deny=VIEW))
    if tester_role:
        if access == "testers":
            out.append(_overwrite(tester_role, 0, allow=TESTER_TALK))
        elif access == "testers_read":
            out.append(_overwrite(tester_role, 0, allow=READ_ONLY, deny=SEND | PUBLIC_THREADS))
        elif access == "staff":
            out.append(_overwrite(tester_role, 0, deny=VIEW))
    for role_id in staff_roles:
        out.append(_overwrite(role_id, 0, allow=STAFF))
    for bot_id in bots:
        out.append(_overwrite(bot_id, 1, allow=BOT))
    return out


def step_layout(apply: bool) -> int:
    token = _flag("DEV_TOKEN")
    if not token:
        sys.exit("DEV_TOKEN is not set")
    api = Discord(token)
    guild = str(DEV_GUILD_ID)
    me = api.call("GET", "/users/@me")
    print(f"layout for guild {guild} as {me.get('username')} ({me['id']})")

    roles = api.call("GET", f"/guilds/{guild}/roles")
    by_name = {r["name"].lower(): r for r in roles}
    tester = by_name.get(ROLE_NAME.lower())
    if tester is None:
        _say(apply, f"create role {ROLE_NAME!r}")
        if apply:
            tester = api.call("POST", f"/guilds/{guild}/roles", {
                "name": ROLE_NAME, "color": ROLE_COLOR, "hoist": True,
                "mentionable": False, "permissions": "0",
            })
    else:
        print(f"  role {ROLE_NAME!r} exists ({tester['id']})")
    tester_id = tester["id"] if tester else None
    staff_roles = [by_name[n.lower()]["id"] for n in STAFF_ROLE_NAMES if n.lower() in by_name]

    bots = []
    for bot_id in dict.fromkeys((str(me["id"]),) + KNOWN_DEV_BOT_IDS):
        try:
            api.call("GET", f"/guilds/{guild}/members/{bot_id}")
            bots.append(bot_id)
        except DiscordError:
            print(f"  note: bot {bot_id} is not in the guild; no overwrite for it")

    channels = api.call("GET", f"/guilds/{guild}/channels")
    category = next((c for c in channels if c["type"] == CATEGORY
                     and c["name"].lower() == CATEGORY_NAME.lower()), None)
    category_overwrites = [_overwrite(guild, 0, deny=VIEW)]
    if tester_id:
        category_overwrites.append(_overwrite(tester_id, 0, allow=VIEW))
    category_overwrites += [_overwrite(r, 0, allow=STAFF) for r in staff_roles]
    category_overwrites += [_overwrite(b, 1, allow=BOT) for b in bots]
    if category is None:
        _say(apply, f"create category {CATEGORY_NAME!r}")
        if apply:
            category = api.call("POST", f"/guilds/{guild}/channels", {
                "name": CATEGORY_NAME, "type": CATEGORY,
                "permission_overwrites": category_overwrites,
            })
    elif _overwrite_set(category.get("permission_overwrites")) != _overwrite_set(category_overwrites):
        _say(apply, f"update permissions on category {CATEGORY_NAME!r}")
        if apply:
            api.call("PATCH", f"/channels/{category['id']}", {"permission_overwrites": category_overwrites})
    else:
        print(f"  category {CATEGORY_NAME!r} is in place ({category['id']})")

    ids = {}
    for key, name, kind, access, topic in CHANNELS:
        existing = None
        if category is not None:
            existing = next((c for c in channels if c.get("parent_id") == category["id"]
                             and c["name"] == name), None)
        overwrites = _channel_overwrites(access, guild, tester_id, staff_roles, bots)
        wanted = {"name": name, "topic": topic, "permission_overwrites": overwrites}
        if kind == FORUM:
            current_tags = {t["name"].lower(): t for t in (existing or {}).get("available_tags") or ()}
            tags = list((existing or {}).get("available_tags") or ())
            tags += [{"name": t} for t in FORUM_TAGS if t not in current_tags]
            wanted["available_tags"] = tags
        if existing is None:
            _say(apply, f"create #{name} ({access})")
            if apply:
                created = api.call("POST", f"/guilds/{guild}/channels",
                                   {**wanted, "type": kind, "parent_id": category["id"]})
                ids[key] = created["id"]
            continue
        ids[key] = existing["id"]
        changed = (
            (existing.get("topic") or "") != topic
            or _overwrite_set(existing.get("permission_overwrites")) != _overwrite_set(overwrites)
            or (kind == FORUM and len(wanted["available_tags"]) != len(existing.get("available_tags") or ()))
        )
        if changed:
            _say(apply, f"update #{name} ({access})")
            if apply:
                api.call("PATCH", f"/channels/{existing['id']}", wanted)
        else:
            print(f"  #{name} is in place ({existing['id']})")

    if apply and ids.get("start_here"):
        recent = api.call("GET", f"/channels/{ids['start_here']}/messages?limit=10") or []
        if not any(m.get("author", {}).get("id") == str(me["id"]) for m in recent):
            mentions = {key: f"<#{cid}>" for key, cid in ids.items()}
            api.call("POST", f"/channels/{ids['start_here']}/messages", {
                "content": START_HERE_MESSAGE.format(**mentions),
                "allowed_mentions": {"parse": []},
            })
            print("  posted the #start-here introduction")

    if apply:
        LAYOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        LAYOUT_PATH.write_text(json.dumps({
            "guild_id": guild, "updated_at": stamp, "role_id": tester_id,
            "category_id": category["id"] if category else None, "channels": ids,
        }, indent=2) + "\n")
        DEV_ROLE_MAP_PATH.write_text(json.dumps({
            "guild_id": guild, "updated_at": stamp, "roles": {"bug_tester": tester_id},
        }, indent=2) + "\n")
        print(f"  wrote {LAYOUT_PATH.relative_to(PROJECT_ROOT)} and "
              f"{DEV_ROLE_MAP_PATH.relative_to(PROJECT_ROOT)}")
    return 0


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #
def _set_config(s, group_id: int, key: str, value: str, apply: bool) -> None:
    """Make every ``key`` row of the group hold ``value``. Reads only in a dry run."""
    from db.models import GroupConfiguration

    rows = (s.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == key)
            .all())
    if not rows:
        _say(apply, f"group {group_id}: {key} = {value!r} (new)")
        if apply:
            s.add(GroupConfiguration(group_id=group_id, config_key=key, config_value=value,
                                     updated_at=datetime.now()))
        return
    for row in rows:
        if row.config_value != value:
            _say(apply, f"group {group_id}: {key} {row.config_value!r} -> {value!r}")
            if apply:
                row.config_value = value
                row.updated_at = datetime.now()


def _spec_config(spec: dict, channels: dict) -> dict:
    """Everything the spec writes on top of the template, as stored text."""
    from utils.dev_guild_guard import allowed_user_ids

    out = {}
    for key, channel_key in spec["channels"].items():
        value = str(channels.get(channel_key) or "") if channel_key else ""
        if channel_key and not value:
            print(f"  warning: the layout has no #{channel_key}; {key} left blank")
        out[key] = value
    out.update(spec["values"])
    out["clan_name"] = spec["name"]
    out["authed_users"] = json.dumps([str(u) for u in sorted(allowed_user_ids())])
    return out


def step_groups(apply: bool) -> int:
    from db.badge_groups import desired_player_ids, sync_badge_group
    from db.models import Group, GroupConfiguration, Guild, Session, user_group_association

    if not LAYOUT_PATH.exists():
        sys.exit(f"{LAYOUT_PATH.relative_to(PROJECT_ROOT)} is missing: run `layout --apply` first")
    layout = json.loads(LAYOUT_PATH.read_text())
    channels = layout.get("channels") or {}
    guild = str(DEV_GUILD_ID)
    print("groups")

    with Session() as s:
        template = (s.query(GroupConfiguration.config_key, GroupConfiguration.config_value)
                    .filter(GroupConfiguration.group_id == 1).all())
        for spec in GROUP_SPECS:
            gid = spec["group_id"]
            wanted = _spec_config(spec, channels)
            group = s.get(Group, gid)
            if group is None:
                _say(apply, f"create group {gid} {spec['name']!r}: the template's "
                            f"{len(template)} config rows, then {len(wanted)} of its own")
                if not apply:
                    continue
                group = Group(group_name=spec["name"], wom_id=None, guild_id=guild,
                              description=spec["description"])
                group.group_id = gid
                s.add(group)
                s.flush()
                seen = set()
                for key, value in template:
                    if key in seen or key in wanted:
                        continue
                    seen.add(key)
                    if key == "export_api_key":
                        value = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                    s.add(GroupConfiguration(group_id=gid, config_key=key, config_value=value or "",
                                             updated_at=datetime.now()))
                for key, value in wanted.items():
                    s.add(GroupConfiguration(group_id=gid, config_key=key, config_value=value,
                                             updated_at=datetime.now()))
                s.flush()
                continue

            for attr, value in (("group_name", spec["name"]), ("guild_id", guild),
                                ("wom_id", None), ("description", spec["description"])):
                if getattr(group, attr) != value:
                    _say(apply, f"group {gid}: {attr} {getattr(group, attr)!r} -> {value!r}")
                    if apply:
                        setattr(group, attr, value)
            for key, value in wanted.items():
                _set_config(s, gid, key, value, apply)

        # One group per guild answers guild-scoped commands: Bug Testers.
        row = s.get(Guild, guild)
        if row is None:
            _say(apply, f"map guild {guild} -> group {BUG_TESTERS_GROUP_ID}")
            if apply:
                s.add(Guild(guild_id=guild, group_id=BUG_TESTERS_GROUP_ID, date_added=datetime.now()))
        elif row.group_id != BUG_TESTERS_GROUP_ID:
            _say(apply, f"map guild {guild}: group {row.group_id} -> {BUG_TESTERS_GROUP_ID}")
            if apply:
                row.group_id = BUG_TESTERS_GROUP_ID

        # Any other group in the dev guild (the old production test group) would
        # announce the same testers a second time.
        reserved = [spec["group_id"] for spec in GROUP_SPECS]
        for other in (s.query(Group).filter(Group.guild_id == guild,
                                            Group.group_id.notin_(reserved)).all()):
            _say(apply, f"detach group {other.group_id} {other.group_name!r} from the dev guild")
            if apply:
                other.guild_id = None

        if apply:
            s.flush()
            added, removed = sync_badge_group(s, BUG_TESTERS_GROUP_ID, BADGE_KEY)
            _say(apply, f"Bug Testers membership: +{added} / -{removed} accounts "
                        f"(from the {BADGE_KEY} badge)")
            s.commit()
        else:
            uga = user_group_association
            wanted_ids = desired_player_ids(s, BADGE_KEY)
            current = {pid for (pid,) in s.query(uga.c.player_id)
                       .filter(uga.c.group_id == BUG_TESTERS_GROUP_ID, uga.c.player_id.isnot(None))}
            _say(apply, f"Bug Testers membership: +{len(wanted_ids - current)} / "
                        f"-{len(current - wanted_ids)} accounts (from the {BADGE_KEY} badge)")
            s.rollback()
    return 0


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Set up the DropTracker dev instance.")
    parser.add_argument("step", choices=("check", "id-offset", "layout", "groups", "all"))
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args(argv)

    if args.step == "check":
        return step_check()
    require_dev()
    print("APPLY" if args.apply else "DRY RUN — nothing is changed without --apply")
    if args.step in ("id-offset", "all"):
        step_id_offset(args.apply)
    if args.step in ("layout", "all"):
        step_layout(args.apply)
    if args.step in ("groups", "all"):
        if args.step == "all" and not args.apply and not LAYOUT_PATH.exists():
            print("groups: skipped in this dry run (the layout has not been written yet)")
        else:
            step_groups(args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
