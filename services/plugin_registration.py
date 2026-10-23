"""Registering a group from inside the RuneLite plugin.

The whole journey stays in the game client, except one trip through Discord:

1. The plugin sends the group's name and either its WiseOldMan group id or
   its clan roster (``start``, from api/routes/plugin_admin.py). The roster
   is for leaders without a WOM group: we create one for them.
2. The plugin opens the website's Discord sign-in with the bot added
   (``authorize_path``). Adding a bot to a server needs Manage Server there,
   and signing in gives us the leader's Discord account and fresh list of the
   servers they manage, so this one step proves everything the website's
   own group wizard asks for.
3. The website's ``/register-group/<id>`` page calls ``complete`` with the
   server Discord says the bot was added to. The group is created through
   the same ``create_web_group`` every other path uses, the game account is
   claimed for the leader, and the plugin gets a group-admin session.
4. The plugin, polling ``status``, picks the session up once and opens the
   group manager.

Pending registrations live in Redis for 30 minutes, keyed by a random id the
plugin and the website both hold; nothing is written to the database until
``complete``.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Optional

TTL_SECONDS = 30 * 60
MAX_ROSTER = 1000
_KEY = "plugreg:{}"
_ACCOUNT_KEY = "plugreg:acc:{}"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
_RSN_RE = re.compile(r"^[A-Za-z0-9 _-]{1,12}$")

EXPIRED = "This registration has expired. Start again from RuneLite."


class RegistrationError(Exception):
    """A sentence for the leader, and which field it is about (or None)."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message, self.field = message, field


def is_id(value) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def authorize_path(reg_id: str) -> str:
    """The website link that signs in with Discord and adds the bot."""
    return f"/api/auth/login?bot=1&redirect=/register-group/{reg_id}"


def clean_roster(roster) -> list:
    """``[{"name", "rank"}]`` from what the plugin sent: valid RSNs, once each."""
    out, seen = [], set()
    for entry in roster if isinstance(roster, list) else []:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").replace(" ", " ").split())
        key = name.lower().replace("_", " ").replace("-", " ")
        if not _RSN_RE.fullmatch(name) or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "rank": str(entry.get("rank") or "")[:40]})
        if len(out) >= MAX_ROSTER:
            break
    return out


def wom_role(rank: str) -> str:
    """The WiseOldMan role for an in-game clan title, or "member"."""
    import wom

    value = re.sub(r"[^a-z]+", "_", (rank or "").lower()).strip("_")
    return value if value in {r.value for r in wom.GroupRole} else "member"


def _load(rc, reg_id: str) -> Optional[dict]:
    if not is_id(reg_id):
        return None
    raw = rc.get(_KEY.format(reg_id))
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except ValueError:
        return None


def _save(rc, reg_id: str, data: dict) -> None:
    rc.set(_KEY.format(reg_id), json.dumps(data), ex=TTL_SECONDS)


def start(rc, *, acc_hash: str, player_name: str, name, wom_id, roster) -> dict:
    """Check what the plugin sent and hold it. Raises RegistrationError."""
    from db.group_rename import GroupRenameError, normalize_group_name

    try:
        name = normalize_group_name(name)
    except GroupRenameError as e:
        raise RegistrationError(str(e), "name")
    members = clean_roster(roster)
    if wom_id is None:
        if not members:
            raise RegistrationError("We couldn't read your clan's members. Open your clan settings "
                                    "in game once, or enter your WiseOldMan group instead.", "wom")
    else:
        try:
            wom_id = int(wom_id)
        except (TypeError, ValueError):
            raise RegistrationError("That doesn't look like a WiseOldMan group id.", "wom")
        if wom_id <= 0:
            raise RegistrationError("That doesn't look like a WiseOldMan group id.", "wom")
        members = []
    previous = rc.get(_ACCOUNT_KEY.format(acc_hash))
    if previous:
        rc.delete(_KEY.format(previous.decode() if isinstance(previous, bytes) else previous))
    reg_id = secrets.token_urlsafe(18)
    _save(rc, reg_id, {"acc_hash": str(acc_hash), "player_name": str(player_name)[:12], "name": name,
                       "wom_id": wom_id, "roster": members, "status": "pending",
                       "created": int(time.time())})
    rc.set(_ACCOUNT_KEY.format(acc_hash), reg_id, ex=TTL_SECONDS)
    return {"id": reg_id, "path": authorize_path(reg_id), "expires_in": TTL_SECONDS}


def status(rc, reg_id: str, acc_hash: str) -> dict:
    """What the plugin shows while it waits. The admin session is handed over
    once, to the account that started the registration."""
    data = _load(rc, reg_id)
    if data is None or data.get("acc_hash") != str(acc_hash):
        return {"status": "failed", "message": EXPIRED}
    if data["status"] != "done":
        return {"status": data["status"], "message": data.get("message")}
    out = {"status": "done", "group_id": data.get("group_id"), "group_name": data.get("group_name"),
           "token": data.get("token"), "message": data.get("message")}
    if data.get("token"):
        data["token"] = None
        _save(rc, reg_id, data)
    return out


def preview(rc, reg_id: str) -> Optional[dict]:
    """What the website page shows before (or after) completing."""
    data = _load(rc, reg_id)
    if data is None:
        return None
    return {"name": data["name"], "status": data["status"], "group_id": data.get("group_id"),
            "creates_wom": data.get("wom_id") is None, "members": len(data.get("roster") or [])}


async def _create_wom_group(name: str, roster: list):
    """(wom_id, verification_code) for a new WiseOldMan group of the roster."""
    import wom

    from utils.wiseoldman import client, limiter

    if not await limiter.wait():
        raise RegistrationError("WiseOldMan is busy right now. Try again in a few minutes.")
    await client.start()
    members = [wom.GroupMemberFragment(m["name"], wom.GroupRole(wom_role(m["rank"]))) for m in roster]
    result = await client.groups.create_group(name, *members)
    if not result.is_ok:
        detail = str(getattr(result.unwrap_err(), "message", "") or "")
        raise RegistrationError("WiseOldMan wouldn't create the group" + (f": {detail}" if detail else ".")
                                + " Try another name, or create it on wiseoldman.net and use its id.")
    created = result.unwrap()
    return int(created.group.id), str(created.verification_code)


async def complete(rc, reg_id: str, *, user_id: int, discord_id: str, username: str, guild_id: str) -> dict:
    """Create the group for a pending registration. The caller has checked
    the user manages ``guild_id``. Idempotent once done (a page refresh)."""
    from db import group_verification_codes as gvc
    from db import plugin_admin_sessions as sessions
    from db.group_creation import create_web_group
    from db.models import AuditLog, GroupConfiguration, Player, Session
    from db.player_claims import claim_player

    data = _load(rc, reg_id)
    if data is None:
        raise RegistrationError(EXPIRED)
    if data["status"] == "done":
        return {"group_id": data["group_id"], "group_name": data["group_name"], "verification_code": None}

    wom_id, wom_code = data.get("wom_id"), None
    if wom_id is None:
        wom_id, wom_code = await _create_wom_group(data["name"], data.get("roster") or [])
    result = await create_web_group(group_name=data["name"], wom_id=wom_id, guild_id=str(guild_id),
                                    owner_discord_id=str(discord_id), owner_username=username,
                                    created_via="plugin")
    if not result.get("success"):
        raise RegistrationError({
            "guild_conflict": "That Discord server already has a DropTracker group.",
            "already_registered": "That Discord server already has a DropTracker group.",
            "wom_conflict": "That WiseOldMan group is already registered on the DropTracker.",
            "invalid_wom": "WiseOldMan doesn't know that group id.",
            "invalid_name": "That group name isn't allowed.",
        }.get(result.get("status"), result.get("message") or "The group couldn't be created. Try again."))
    group_id = int(result["group_id"])

    s = Session()
    try:
        if wom_code:
            s.add(GroupConfiguration(group_id=group_id, config_key="wom_verification_code", config_value=wom_code))
        code = gvc.issue_code(s, group_id, user_id, "plugin")
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id, action="group.create.plugin",
                       target=f"group:{group_id}", before=None, after=data["player_name"]))
        s.commit()
        player = s.query(Player).filter(Player.account_hash == data["acc_hash"]).first()
        rsn = player.player_name if player is not None else None
        owner = player.user_id if player is not None else None
    finally:
        s.close()

    message, token = None, None
    if rsn and owner is None:
        claimed = claim_player(rsn, discord_id=str(discord_id), username=username, guild_id=str(guild_id))
        owner = user_id if claimed.get("status") in ("claimed", "already_yours") else None
    if owner is not None and int(owner) == int(user_id):
        s = Session()
        try:
            token = sessions.mint(s, group_id, user_id, data["acc_hash"], sessions.current_generation(s, group_id))
            s.commit()
        finally:
            s.close()
    else:
        message = ("Your group is registered. This game account isn't linked to the Discord account you "
                   "used, so manage the group on droptracker.io.")
    data.update(status="done", group_id=group_id, group_name=result.get("group_name") or data["name"],
                token=token, message=message, roster=[])
    _save(rc, reg_id, data)
    return {"group_id": group_id, "group_name": data["group_name"], "verification_code": code}
