"""The Bug Tester roster, and how production hands it to the dev instance.

Who is a bug tester is decided in production and only there: an active
``bug_tester_helper`` badge on any of a user's accounts (``/bug-tester``, the
admin badge page — see services/discord_roles.py). Two things outside
production need that answer, and both get it from here:

* **The Cloudflare Worker** mirrors testers' submissions to dev. It learns who
  they are as keyed digests of their account hashes, served by /edge-config
  (services/edge_config.py).
* **The dev instance** runs on a production dump, which is stale the day it is
  restored. ``workers/dev_sync.py`` pushes the roster to it within a second or
  two of any change, and ``api/routes/dev_sync.py`` applies it there.

The roster is always a full snapshot, never a diff, so a push that is lost,
repeated, or applied to a freshly restored dev database converges on the same
state. It carries only what dev needs to recognise a tester: the user row
(never the auth token), every account on it, the active awards and the badge
definition. It travels sealed with Fernet (``DEV_SYNC_KEY``): the account hash
is what the plugin endpoints identify a player by, so it is not sent readable.

Like services/discord_roles.py this imports neither ``interactions`` nor
``web_api``, and all database access is lazy, so it is unit-testable alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

#: Same value as services.discord_roles.BUG_TESTER_BADGE_KEY (a test pins it);
#: not imported, so this module stays importable without that one's env reads.
BUG_TESTER_BADGE_KEY = "bug_tester_helper"

#: Pushed to by anything that changed the roster; workers/dev_sync.py blocks on it.
REQUEST_KEY = "devsync:testers:requested"
#: /edge-config's cached tester digests (services/edge_config.py).
DIGEST_CACHE_KEY = "edge:testers:digests"

# Dev-side state written when a roster is applied.
#: Discord ids of current testers, read by utils/dev_guild_guard.user_allowed().
DEV_DISCORD_IDS_KEY = "devsync:testers:discord_ids"
#: ``ts`` of the newest snapshot applied, so an older one cannot undo it.
DEV_LAST_TS_KEY = "devsync:testers:last_ts"
#: Held while a snapshot is being applied (one writer at a time).
DEV_APPLY_LOCK_KEY = "devsync:testers:apply_lock"

SNAPSHOT_VERSION = 1
#: How old a sealed snapshot may be when it arrives. Fernet checks this.
MAX_TOKEN_AGE_SECONDS = 300

_BADGE_FIELDS = ("key", "name", "description", "icon_url", "icon_emoji", "tone",
                 "semantic", "scope", "active", "criteria")
_PLAYER_FIELDS = ("player_id", "player_name", "account_hash", "wom_id", "user_id",
                  "total_level", "log_slots", "account_type")


def _env(name: str) -> str:
    """An env var, tolerant of the quoting this repo's .env files use."""
    return (os.getenv(name) or "").strip().strip('"').strip("'")


def sync_key() -> Optional[str]:
    """The Fernet key shared by production and the dev instance, if configured."""
    return _env("DEV_SYNC_KEY") or None


def sync_url() -> Optional[str]:
    """Where production pushes the roster (dev's POST /dev-sync/testers)."""
    return _env("DEV_SYNC_URL") or None


# --------------------------------------------------------------------------- #
# Building the snapshot (production)
# --------------------------------------------------------------------------- #
def _iso(value) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def empty_roster() -> Dict[str, Any]:
    return {"v": SNAPSHOT_VERSION, "ts": time.time(), "badge": None,
            "users": [], "players": [], "awards": []}


def load_roster(session, badge_key: str = BUG_TESTER_BADGE_KEY) -> Dict[str, Any]:
    """Everything the dev instance needs about current testers, as plain data.

    A tester is a user with an active award of an *active* badge on one of
    their linked accounts — the same rule as services.discord_roles. Every
    account on that user is included, not just the badged one: the mirror
    follows the person.
    """
    from db.models import Badge, Player, PlayerBadge, User

    roster = empty_roster()
    badge = session.query(Badge).filter(Badge.key == badge_key).first()
    if badge is None:
        return roster
    roster["badge"] = {name: getattr(badge, name, None) for name in _BADGE_FIELDS}
    roster["badge"]["active"] = bool(badge.active)
    if not badge.active:
        return roster

    award_rows = (
        session.query(PlayerBadge.player_id, PlayerBadge.slot_key, PlayerBadge.group_key,
                      PlayerBadge.awarded_at, PlayerBadge.context, Player.user_id)
        .join(Player, Player.player_id == PlayerBadge.player_id)
        .filter(
            PlayerBadge.badge_id == badge.badge_id,
            PlayerBadge.status == "active",
            Player.user_id.isnot(None),
        )
        .all()
    )
    user_ids = sorted({int(row.user_id) for row in award_rows})
    if not user_ids:
        return roster

    roster["awards"] = sorted(
        (
            {
                "player_id": int(row.player_id),
                "slot_key": row.slot_key or "",
                "group_key": int(row.group_key or 0),
                "awarded_at": _iso(row.awarded_at),
                "context": row.context,
            }
            for row in award_rows
        ),
        key=lambda a: (a["player_id"], a["group_key"], a["slot_key"]),
    )
    roster["users"] = [
        {"user_id": int(uid), "discord_id": str(did) if did else None, "username": name}
        for uid, did, name in (
            session.query(User.user_id, User.discord_id, User.username)
            .filter(User.user_id.in_(user_ids))
            .order_by(User.user_id)
            .all()
        )
    ]
    players = (
        session.query(*[getattr(Player, name) for name in _PLAYER_FIELDS])
        .filter(Player.user_id.in_(user_ids))
        .order_by(Player.player_id)
        .all()
    )
    roster["players"] = [
        {name: (str(value).strip() if name == "account_hash" and value is not None else value)
         for name, value in zip(_PLAYER_FIELDS, row)}
        for row in players
    ]
    return roster


def fingerprint(roster: Dict[str, Any]) -> str:
    """Content hash of a snapshot, ignoring when it was taken."""
    body = {k: v for k, v in roster.items() if k != "ts"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def account_hashes(roster: Dict[str, Any]) -> List[str]:
    """Every tester account hash, exactly as the plugin sends it."""
    return sorted({
        str(p["account_hash"]).strip()
        for p in roster.get("players") or ()
        if p.get("account_hash") not in (None, "") and str(p["account_hash"]).strip()
    })


def current_account_hashes() -> List[str]:
    """account_hashes() for the live roster, in a session of its own."""
    from db.models import Session

    with Session() as session:
        return account_hashes(load_roster(session))


# --------------------------------------------------------------------------- #
# Change signal
# --------------------------------------------------------------------------- #
def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def notify_changed() -> bool:
    """Tell production's consumers the roster may have changed. Never raises.

    Wakes workers/dev_sync.py for an immediate push and drops the cached
    /edge-config digests, so the Worker sees a new tester on its next poll.
    Call it after the commit, not before: the push re-reads the database.
    """
    client = _redis()
    if client is None:
        return False
    try:
        pipe = client.pipeline()
        pipe.rpush(REQUEST_KEY, "1")
        pipe.ltrim(REQUEST_KEY, -10, -1)
        pipe.delete(DIGEST_CACHE_KEY)
        pipe.execute()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Sealing
# --------------------------------------------------------------------------- #
class InvalidSnapshot(ValueError):
    """The sealed payload could not be opened, or is not a roster."""


def _fernet(key: str):
    from cryptography.fernet import Fernet

    return Fernet(key.encode("ascii") if isinstance(key, str) else key)


def seal(roster: Dict[str, Any], key: str) -> str:
    payload = json.dumps(roster, sort_keys=True, separators=(",", ":"), default=str)
    return _fernet(key).encrypt(payload.encode("utf-8")).decode("ascii")


def unseal(token: str, key: str, max_age: int = MAX_TOKEN_AGE_SECONDS) -> Dict[str, Any]:
    """Open and shape-check a sealed roster. Raises InvalidSnapshot."""
    from cryptography.fernet import InvalidToken

    try:
        raw = _fernet(key).decrypt(token.strip().encode("ascii"), ttl=max_age)
    except (InvalidToken, ValueError, TypeError) as exc:
        raise InvalidSnapshot("the token is invalid, expired or sealed with another key") from exc
    try:
        roster = json.loads(raw)
    except ValueError as exc:
        raise InvalidSnapshot("the payload is not JSON") from exc
    if not isinstance(roster, dict) or roster.get("v") != SNAPSHOT_VERSION:
        raise InvalidSnapshot("unsupported snapshot version")
    for name in ("users", "players", "awards"):
        if not isinstance(roster.get(name), list):
            raise InvalidSnapshot(f"'{name}' must be a list")
    if roster.get("badge") is not None and not isinstance(roster.get("badge"), dict):
        raise InvalidSnapshot("'badge' must be an object")
    try:
        roster["ts"] = float(roster.get("ts"))
    except (TypeError, ValueError) as exc:
        raise InvalidSnapshot("'ts' must be a number") from exc
    return roster


# --------------------------------------------------------------------------- #
# Applying the snapshot (dev instance)
# --------------------------------------------------------------------------- #
@dataclass
class ApplyResult:
    users_added: int = 0
    users_updated: int = 0
    players_added: int = 0
    players_updated: int = 0
    awards_added: int = 0
    awards_revoked: int = 0
    #: Rows that could not be applied, with the reason. Reported, never fatal.
    skipped: List[str] = field(default_factory=list)
    #: Dev user ids that hold the badge after this snapshot.
    tester_user_ids: List[int] = field(default_factory=list)
    #: Their Discord ids.
    tester_discord_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "users_added": self.users_added,
            "users_updated": self.users_updated,
            "players_added": self.players_added,
            "players_updated": self.players_updated,
            "awards_added": self.awards_added,
            "awards_revoked": self.awards_revoked,
            "skipped": list(self.skipped),
            "testers": len(self.tester_user_ids),
        }


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


def _apply_badge(session, spec: Dict[str, Any]):
    from db.models import Badge

    badge = session.query(Badge).filter(Badge.key == spec["key"]).first()
    values = {name: spec.get(name) for name in _BADGE_FIELDS if name != "key"}
    values["active"] = bool(values.get("active"))
    for required, fallback in (("name", spec["key"]), ("description", ""), ("tone", "gold"),
                               ("semantic", "permanent"), ("scope", "global")):
        if values.get(required) is None:
            values[required] = fallback
    if badge is None:
        badge = Badge(key=spec["key"], **values)
        session.add(badge)
    else:
        for name, value in values.items():
            setattr(badge, name, value)
    session.flush()
    return badge


def _error_text(exc) -> str:
    return str(getattr(exc, "orig", None) or exc)


def _apply_users(session, users, result: ApplyResult) -> Dict[int, int]:
    """Upsert tester users. Returns ``{production user_id: dev user_id}``.

    Ids usually match, because dev is a production dump. They stop matching
    for anyone created after the dump: dev hands out the same auto-increment
    ids to its own rows, so a production id can already belong to someone
    else here. The Discord id is the identity; the row id is only a hint.
    """
    from sqlalchemy.exc import IntegrityError

    from db.models import User

    user_map: Dict[int, int] = {}
    for spec in users:
        prod_id = int(spec["user_id"])
        discord_id = str(spec.get("discord_id") or "").strip() or None
        username = spec.get("username")
        try:
            with session.begin_nested():
                row = (session.query(User).filter(User.discord_id == discord_id).first()
                       if discord_id else None)
                if row is None:
                    by_id = session.get(User, prod_id)
                    if by_id is not None and by_id.discord_id in (None, ""):
                        row = by_id  # the dump's copy, from before it had a Discord id
                if row is not None:
                    row.discord_id = discord_id or row.discord_id
                    if username:
                        row.username = username
                    result.users_updated += 1
                else:
                    taken = session.get(User, prod_id) is not None
                    row = User(discord_id=discord_id, username=username,
                               auth_token=secrets.token_hex(8))
                    if not taken:
                        row.user_id = prod_id
                    session.add(row)
                    result.users_added += 1
                session.flush()
                user_map[prod_id] = int(row.user_id)
        except IntegrityError as exc:
            result.skipped.append(f"user {prod_id}: {_error_text(exc)}")
    return user_map


def _norm_name(value) -> str:
    return " ".join(str(value or "").replace("_", " ").replace("-", " ").split()).lower()


def _resolve_player(session, spec: Dict[str, Any], account_hash, wom_id):
    """The dev row that is this account, ``"new"``, or ``(None, reason)``.

    The account hash is the strongest identity, then the WOM id; the row id is
    trusted only when the row there has no identity that contradicts it.
    """
    from db.models import Player

    pid = int(spec["player_id"])
    by_hash = (session.query(Player).filter(Player.account_hash == account_hash).first()
               if account_hash else None)
    by_wom = (session.query(Player).filter(Player.wom_id == wom_id).first()
              if wom_id is not None else None)
    if by_hash is not None and by_wom is not None and by_hash.player_id != by_wom.player_id:
        return None, (f"its account hash is on dev player {by_hash.player_id} "
                      f"but its WOM id is on dev player {by_wom.player_id}")
    anchor = by_hash or by_wom
    if anchor is not None:
        return anchor, None
    by_id = session.get(Player, pid)
    if by_id is None:
        return "new", None
    contradicts = (
        (by_id.account_hash not in (None, "") and by_id.account_hash != account_hash)
        or (by_id.wom_id is not None and by_id.wom_id != wom_id)
        or (not by_id.account_hash and by_id.wom_id is None
            and _norm_name(by_id.player_name) != _norm_name(spec.get("player_name")))
    )
    return ("new", None) if contradicts else (by_id, None)


def _apply_players(session, players, user_map: Dict[int, int], result: ApplyResult) -> Dict[int, int]:
    """Upsert tester accounts. Returns ``{production player_id: dev player_id}``."""
    from sqlalchemy.exc import IntegrityError

    from db.models import Player

    player_map: Dict[int, int] = {}
    for spec in players:
        pid = int(spec["player_id"])
        owner = user_map.get(int(spec["user_id"])) if spec.get("user_id") is not None else None
        if owner is None:
            result.skipped.append(f"player {pid}: its user was not applied")
            continue
        raw_hash = spec.get("account_hash")
        account_hash = str(raw_hash).strip() if raw_hash not in (None, "") else None
        account_hash = account_hash or None
        wom_id = spec.get("wom_id")
        try:
            with session.begin_nested():
                row, reason = _resolve_player(session, spec, account_hash, wom_id)
                if reason:
                    result.skipped.append(f"player {pid} ({spec.get('player_name')}): {reason}")
                    continue
                if row == "new":
                    taken = session.get(Player, pid) is not None
                    row = Player(wom_id=wom_id, player_name=spec.get("player_name"),
                                 account_hash=account_hash, user_id=owner,
                                 log_slots=spec.get("log_slots") or 0,
                                 total_level=spec.get("total_level") or 0)
                    if not taken:
                        row.player_id = pid
                    session.add(row)
                    result.players_added += 1
                else:
                    row.player_name = spec.get("player_name") or row.player_name
                    if account_hash:
                        row.account_hash = account_hash
                    if wom_id is not None:
                        row.wom_id = wom_id
                    row.user_id = owner
                    for name in ("total_level", "log_slots"):
                        if spec.get(name) is not None:
                            setattr(row, name, spec[name])
                    result.players_updated += 1
                if spec.get("account_type"):
                    row.account_type = spec["account_type"]
                session.flush()
                player_map[pid] = int(row.player_id)
        except IntegrityError as exc:
            result.skipped.append(f"player {pid}: {_error_text(exc)}")
    return player_map


def _apply_awards(session, badge, awards, player_map: Dict[int, int], result: ApplyResult) -> None:
    """Make the badge's active awards on dev match the snapshot exactly."""
    from sqlalchemy.exc import IntegrityError

    from db.models import PlayerBadge

    wanted: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
    for spec in awards:
        dev_pid = player_map.get(int(spec["player_id"]))
        if dev_pid is None:
            continue
        slot_key = spec.get("slot_key") or f"p:{dev_pid}"
        if slot_key == f"p:{int(spec['player_id'])}":
            slot_key = f"p:{dev_pid}"
        wanted[(dev_pid, int(spec.get("group_key") or 0), slot_key)] = spec

    now = datetime.now()
    held = set()
    for award in (session.query(PlayerBadge)
                  .filter(PlayerBadge.badge_id == badge.badge_id, PlayerBadge.status == "active")
                  .all()):
        slot = (int(award.player_id), int(award.group_key or 0), award.slot_key or "")
        if slot in wanted and badge.active:
            held.add(slot)
            continue
        award.status = "revoked"
        award.active_key = None
        award.lost_at = now
        result.awards_revoked += 1
    session.flush()

    if not badge.active:
        return
    for slot, spec in wanted.items():
        if slot in held:
            continue
        pid, group_key, slot_key = slot
        try:
            with session.begin_nested():
                session.add(PlayerBadge(
                    badge_id=badge.badge_id, player_id=pid,
                    group_id=group_key or None, group_key=group_key,
                    status="active", slot_key=slot_key, active_key=slot_key,
                    awarded_at=_parse_dt(spec.get("awarded_at")) or now,
                    awarded_by=None, context=spec.get("context"),
                ))
                session.flush()
            result.awards_added += 1
        except IntegrityError as exc:
            result.skipped.append(f"award for player {pid}: {_error_text(exc)}")


def apply_roster(session, roster: Dict[str, Any]) -> ApplyResult:
    """Bring this (dev) database's view of the testers in line with ``roster``.

    The caller commits. Rows that cannot be applied (a unique-key clash with a
    row created on dev) are skipped and reported rather than overwritten.
    """
    from db.models import Badge, Player, PlayerBadge, User

    result = ApplyResult()
    spec = roster.get("badge")
    if not spec or not spec.get("key"):
        # Production has no such badge, so nobody is a tester. Keep any dev
        # definition but clear its awards.
        badge = session.query(Badge).filter(Badge.key == BUG_TESTER_BADGE_KEY).first()
        if badge is not None:
            _apply_awards(session, badge, [], {}, result)
        return result

    badge = _apply_badge(session, spec)
    user_map = _apply_users(session, roster.get("users") or [], result)
    player_map = _apply_players(session, roster.get("players") or [], user_map, result)
    _apply_awards(session, badge, roster.get("awards") or [], player_map, result)

    # Read back who holds it now, by the same rule services.discord_roles uses.
    rows = (
        session.query(User.user_id, User.discord_id)
        .join(Player, Player.user_id == User.user_id)
        .join(PlayerBadge, PlayerBadge.player_id == Player.player_id)
        .filter(PlayerBadge.badge_id == badge.badge_id, PlayerBadge.status == "active")
        .distinct()
        .all()
    ) if badge.active else []
    result.tester_user_ids = sorted({int(uid) for uid, _ in rows})
    result.tester_discord_ids = sorted({str(did) for _, did in rows if did})
    return result


def _release_lock(client, token: str) -> None:
    try:
        held = client.get(DEV_APPLY_LOCK_KEY)
        if held is not None and (held.decode() if isinstance(held, bytes) else held) == token:
            client.delete(DEV_APPLY_LOCK_KEY)
    except Exception:
        pass


def _after_apply(result: ApplyResult) -> None:
    """Let the rest of the dev instance catch up now rather than on its own timers."""
    try:
        from services.discord_roles import request_sync

        request_sync()  # the dev webhook bot gives or takes the Bug Tester role
    except Exception:
        pass
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        for user_id in result.tester_user_ids:
            invalidate_user_entitlement_cache(user_id)
    except Exception:
        pass


def apply_snapshot(roster: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Dev side: apply one opened snapshot. Returns ``(http_status, body)``.

    One writer at a time (a Redis lock), and never backwards: a snapshot
    older than the newest one applied is acknowledged and ignored, so a
    delayed retry cannot bring back a tester who has since been removed.
    """
    client = _redis()
    token = secrets.token_hex(8)
    if client is not None:
        try:
            if not client.set(DEV_APPLY_LOCK_KEY, token, nx=True, ex=60):
                return 409, {"status": "busy"}
            last = client.get(DEV_LAST_TS_KEY)
            if last is not None and float(last) > float(roster["ts"]):
                _release_lock(client, token)
                return 200, {"status": "stale"}
        except (TypeError, ValueError):
            pass
        except Exception:
            client = None  # Redis is unwell; apply anyway, unlocked.

    try:
        from db.badge_groups import sync_configured
        from db.models import Session

        with Session() as session:
            try:
                result = apply_roster(session, roster)
                groups = sync_configured(session, commit=False)
                session.commit()
            except Exception:
                session.rollback()
                raise

        publish_dev_testers(result.tester_discord_ids)
        if client is not None:
            client.set(DEV_LAST_TS_KEY, repr(float(roster["ts"])))
        _after_apply(result)
        body = result.as_dict()
        body["status"] = "applied"
        body["groups"] = {
            str(group_id): {"added": added, "removed": removed}
            for group_id, (added, removed) in groups.items()
        }
        return 200, body
    finally:
        if client is not None:
            _release_lock(client, token)


def publish_dev_testers(discord_ids: List[str]) -> None:
    """Record the current testers' Discord ids for the dev DM allowlist.

    Replaced in one MULTI/EXEC, so a reader never sees the set half-built.
    """
    client = _redis()
    if client is None:
        return
    pipe = client.pipeline(transaction=True)
    pipe.delete(DEV_DISCORD_IDS_KEY)
    if discord_ids:
        pipe.sadd(DEV_DISCORD_IDS_KEY, *discord_ids)
    pipe.execute()


def dev_tester_discord_ids() -> Set[int]:
    """The ids publish_dev_testers() last recorded. Empty on any failure."""
    client = _redis()
    if client is None:
        return set()
    try:
        members = client.smembers(DEV_DISCORD_IDS_KEY) or ()
    except Exception:
        return set()
    out = set()
    for raw in members:
        try:
            out.add(int(raw.decode() if isinstance(raw, bytes) else raw))
        except (TypeError, ValueError):
            continue
    return out
