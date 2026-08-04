"""Where loot-split tracking is allowed to happen: a global NPC allowlist.

Splits only make sense at sources where a party genuinely shares one loot
event. Everywhere else a "nearby player" is just someone standing next to you
— a slayer cave, a bank, a Guardians of the Rift lobby — and crediting them a
share corrupts group leaderboards. This module holds the **runtime-configurable**
set of ``npc_id`` values at which split tracking may run.

Three modes, stored alongside the list:

  ``off``      no gate at all (legacy behaviour: splits run everywhere)
  ``shadow``   splits still run everywhere, but every split that WOULD have
               been blocked is counted in Redis — impact data before flipping
  ``enforce``  splits only run for allowlisted npc_ids

**Default is ``shadow``**, deliberately: an allowlist that silently discards
real activity is the failure mode this project has hit before (the server-loot
npc id list), so the gate ships observable and inert, and only starts changing
outcomes when someone sets ``enforce`` on purpose.

Storage (durable, no migration): two global-group config rows —
``GroupConfiguration(group_id=2, config_key='split_eligible_npc_ids')`` with the
authoritative JSON in ``long_value``, and ``config_key='split_policy_mode'``.
Same "global config on group 2" precedent as ``utils/pb_blocklist.py``, and the
same per-process TTL cache, so every service picks up admin edits within
``POLICY_TTL`` seconds with no redeploy.

The stored payload is a JSON object of ``{"npc_id": "category"}`` (a bare list
is also accepted). The category is descriptive only — it drives the admin UI
and the review doc, never the gate.
"""
from __future__ import annotations

import json
import time
from threading import Lock
from typing import Dict, Iterable, Optional, Set

from sqlalchemy import bindparam, text

# The "global group" pseudo-clan that already backs site-wide config values.
GLOBAL_GROUP_ID = 2
CONFIG_KEY = "split_eligible_npc_ids"
MODE_KEY = "split_policy_mode"
POLICY_TTL = 30.0  # seconds; matches utils.pb_blocklist / utils.group_config

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)
DEFAULT_MODE = MODE_SHADOW

# Descriptive categories used by the seed + admin UI.
CATEGORY_RAID = "raid"
CATEGORY_RAID_ROOM = "raid_room"
CATEGORY_TEAM_BOSS = "team_boss"
CATEGORY_DUO_BOSS = "duo_boss"

_lock = Lock()
_cache: Optional[Dict[int, str]] = None
_cache_expires: float = 0.0
_mode_cache: Optional[str] = None
_mode_expires: float = 0.0

# Shadow/enforce telemetry. TTL'd so the keyspace self-cleans if the feature is
# abandoned; see services/split_observer.py for the same contract.
_SHADOW_TTL = 60 * 24 * 3600
_BLOCKED_HASH = "splitpolicy:blocked"
_BLOCKED_NAMES = "splitpolicy:blocked:names"
_ALLOWED_HASH = "splitpolicy:allowed"


# --------------------------------------------------------------------------- #
# Parsing / DB access
# --------------------------------------------------------------------------- #
def _parse(raw) -> Dict[int, str]:
    """Parse the stored payload to ``{npc_id: category}``.

    Accepts a JSON object (preferred), a JSON array of ids, or a bare
    comma-separated id string (tolerated for hand edits in the data browser).
    """
    if not raw:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        data = None

    out: Dict[int, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                out[int(k)] = str(v or "").strip() or CATEGORY_TEAM_BOSS
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(data, list):
        for v in data:
            try:
                out[int(v)] = CATEGORY_TEAM_BOSS
            except (TypeError, ValueError):
                continue
        return out

    for part in s.strip("[]{}").split(","):
        part = part.strip().strip('"').strip("'")
        if part:
            try:
                out[int(part)] = CATEGORY_TEAM_BOSS
            except ValueError:
                continue
    return out


def _read_row(session, key: str):
    from db.models import GroupConfiguration

    return (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == GLOBAL_GROUP_ID,
            GroupConfiguration.config_key == key,
        )
        .first()
    )


def _load_from_db(session) -> Dict[int, str]:
    """Authoritative read straight from the DB (bypasses the TTL cache)."""
    row = _read_row(session, CONFIG_KEY)
    if not row:
        return {}
    raw = row.long_value if (row.long_value and str(row.long_value).strip()) else row.config_value
    return _parse(raw)


def _load_mode_from_db(session) -> str:
    row = _read_row(session, MODE_KEY)
    if not row:
        return DEFAULT_MODE
    mode = str(row.config_value or "").strip().lower()
    return mode if mode in VALID_MODES else DEFAULT_MODE


def _fresh_session():
    from db import Session

    return Session()


# --------------------------------------------------------------------------- #
# Read path (hot: called per drop)
# --------------------------------------------------------------------------- #
def get_eligible(session=None) -> Dict[int, str]:
    """``{npc_id: category}`` served from a per-process TTL cache.

    Fails safe-for-availability: any read error yields the last-known map (or
    empty), and an empty map with ``enforce`` set would block every split — so
    ``allows_split`` additionally treats an empty allowlist as "not configured"
    and permits, rather than silently killing the feature on a DB blip.
    """
    global _cache, _cache_expires
    now = time.monotonic()
    with _lock:
        if _cache is not None and now < _cache_expires:
            return dict(_cache)

    own = session is None
    s = _fresh_session() if own else session
    try:
        mapping = _load_from_db(s)
    except Exception:
        with _lock:
            return dict(_cache) if _cache is not None else {}
    finally:
        if own:
            s.close()

    with _lock:
        _cache = dict(mapping)
        _cache_expires = now + POLICY_TTL
        return dict(_cache)


def get_mode(session=None) -> str:
    global _mode_cache, _mode_expires
    now = time.monotonic()
    with _lock:
        if _mode_cache is not None and now < _mode_expires:
            return _mode_cache

    own = session is None
    s = _fresh_session() if own else session
    try:
        mode = _load_mode_from_db(s)
    except Exception:
        with _lock:
            return _mode_cache or DEFAULT_MODE
    finally:
        if own:
            s.close()

    with _lock:
        _mode_cache = mode
        _mode_expires = now + POLICY_TTL
        return mode


def is_eligible(npc_id, session=None) -> bool:
    """Pure allowlist membership, independent of the active mode."""
    if npc_id is None:
        return False
    try:
        npc_id = int(npc_id)
    except (TypeError, ValueError):
        return False
    return npc_id in get_eligible(session)


def category_for(npc_id, session=None) -> Optional[str]:
    try:
        return get_eligible(session).get(int(npc_id))
    except (TypeError, ValueError):
        return None


def is_configured(session=None) -> bool:
    """Whether an allowlist has actually been seeded (empty = not configured)."""
    return bool(get_eligible(session))


def allows_split(npc_id, *, npc_name=None, session=None) -> bool:
    """The gate: may split tracking run for this source right now?

    Decision only — telemetry belongs to :func:`record_split_event`, which the
    caller fires at the moment a split would really have happened.

    Permits when the npc is unknown, when the allowlist has never been seeded,
    or when the mode is anything other than ``enforce``. In other words the
    only way this returns False is a deliberate, configured enforcement.
    """
    if npc_id is None:
        return True
    try:
        npc_id = int(npc_id)
    except (TypeError, ValueError):
        return True
    if get_mode(session) != MODE_ENFORCE:
        return True
    eligible = get_eligible(session)
    if not eligible:
        # Never configured (or an unreadable row) — do not block anything.
        return True
    return npc_id in eligible


def record_split_event(npc_id, npc_name=None, *, session=None, r=None) -> None:
    """Count one split that was about to run, by whether the source is allowed.

    Call this where a split would genuinely have been applied (participants
    resolved, group opted in) — NOT once per drop. The counters then read as
    "real split events enforcement would keep / would stop", which is the whole
    point of shadow mode. Fail-open; never raises into the submission path.
    """
    if npc_id is None:
        return
    try:
        npc_id = int(npc_id)
    except (TypeError, ValueError):
        return
    try:
        if get_mode(session) == MODE_OFF or not is_configured(session):
            return
        _record(npc_id, npc_name, allowed=npc_id in get_eligible(session), r=r)
    except Exception:
        pass


def invalidate() -> None:
    """Drop this process's cached copies (called after a local write)."""
    global _cache, _cache_expires, _mode_cache, _mode_expires
    with _lock:
        _cache = None
        _cache_expires = 0.0
        _mode_cache = None
        _mode_expires = 0.0


# --------------------------------------------------------------------------- #
# Telemetry (fail-open, never raises into the submission path)
# --------------------------------------------------------------------------- #
def _conn(r=None):
    if r is not None:
        if hasattr(r, "pipeline"):
            return r
        return getattr(r, "client", None)
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def _record(npc_id: int, npc_name, *, allowed: bool, r=None) -> None:
    conn = _conn(r)
    if conn is None:
        return
    try:
        pipe = conn.pipeline(transaction=False)
        if allowed:
            pipe.hincrby(_ALLOWED_HASH, str(npc_id), 1)
            pipe.expire(_ALLOWED_HASH, _SHADOW_TTL)
        else:
            pipe.hincrby(_BLOCKED_HASH, str(npc_id), 1)
            pipe.expire(_BLOCKED_HASH, _SHADOW_TTL)
            if npc_name:
                pipe.hset(_BLOCKED_NAMES, str(npc_id), str(npc_name)[:64])
                pipe.expire(_BLOCKED_NAMES, _SHADOW_TTL)
        pipe.execute()
    except Exception:
        pass


def impact_snapshot(r=None) -> dict:
    """``{"blocked": {npc_id: {"name","count"}}, "allowed": {npc_id: count}}``.

    What the gate has seen since the counters were last cleared — i.e. how many
    real split events ``enforce`` would stop, and how many it would keep.
    """
    conn = _conn(r)
    empty = {"blocked": {}, "allowed": {}}
    if conn is None:
        return empty
    try:
        def _decode(h):
            out = {}
            for k, v in (h or {}).items():
                key = k.decode() if isinstance(k, bytes) else str(k)
                val = v.decode() if isinstance(v, bytes) else v
                out[key] = val
            return out

        blocked = _decode(conn.hgetall(_BLOCKED_HASH))
        names = _decode(conn.hgetall(_BLOCKED_NAMES))
        allowed = _decode(conn.hgetall(_ALLOWED_HASH))
        return {
            "blocked": {
                int(k): {"name": names.get(k, ""), "count": int(v or 0)}
                for k, v in blocked.items()
            },
            "allowed": {int(k): int(v or 0) for k, v in allowed.items()},
        }
    except Exception:
        return empty


def clear_impact(r=None) -> None:
    conn = _conn(r)
    if conn is None:
        return
    try:
        conn.delete(_BLOCKED_HASH, _BLOCKED_NAMES, _ALLOWED_HASH)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Write path (superadmin surface + seed script)
# --------------------------------------------------------------------------- #
def _apply(session, mapping: Dict[int, str]) -> None:
    """Upsert the allowlist row WITHOUT committing (caller commits)."""
    from db.models import GroupConfiguration

    ordered = {str(int(k)): str(v or CATEGORY_TEAM_BOSS) for k, v in sorted(mapping.items())}
    payload = json.dumps(ordered)
    summary = f"{len(ordered)} split-eligible NPC(s)"
    row = _read_row(session, CONFIG_KEY)
    if row is None:
        session.add(
            GroupConfiguration(
                group_id=GLOBAL_GROUP_ID,
                config_key=CONFIG_KEY,
                config_value=summary,
                long_value=payload,
            )
        )
    else:
        row.config_value = summary
        row.long_value = payload


def set_eligible(session, mapping: Dict[int, str]) -> Dict[int, str]:
    """Replace the whole allowlist. Returns the stored map."""
    clean = {int(k): str(v or CATEGORY_TEAM_BOSS) for k, v in dict(mapping).items()}
    _apply(session, clean)
    session.commit()
    invalidate()
    return clean


def add_eligible(session, mapping) -> Dict[int, str]:
    """Merge ids into the allowlist. Accepts ``{id: category}`` or an iterable
    of ids (defaulting to the team_boss category). Idempotent."""
    if isinstance(mapping, dict):
        incoming = {int(k): str(v or CATEGORY_TEAM_BOSS) for k, v in mapping.items()}
    else:
        incoming = {int(i): CATEGORY_TEAM_BOSS for i in mapping}
    current = _load_from_db(session)
    current.update(incoming)
    _apply(session, current)
    session.commit()
    invalidate()
    return current


def remove_eligible(session, npc_ids: Iterable[int]) -> Dict[int, str]:
    remove = {int(i) for i in npc_ids}
    current = _load_from_db(session)
    for i in remove:
        current.pop(i, None)
    _apply(session, current)
    session.commit()
    invalidate()
    return current


def set_mode(session, mode: str) -> str:
    """Set the active mode. Raises ValueError on an unknown mode."""
    from db.models import GroupConfiguration

    mode = str(mode or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid split policy mode {mode!r}; expected one of {VALID_MODES}")
    row = _read_row(session, MODE_KEY)
    if row is None:
        session.add(
            GroupConfiguration(
                group_id=GLOBAL_GROUP_ID,
                config_key=MODE_KEY,
                config_value=mode,
            )
        )
    else:
        row.config_value = mode
    session.commit()
    invalidate()
    return mode


def sibling_ids(session, npc_ids) -> Set[int]:
    """Expand ids to every ``npc_list`` id sharing a (case-insensitive) name,
    so allowlisting a boss covers its variant ids. Mirrors pb_blocklist."""
    clean = list({int(i) for i in npc_ids})
    if not clean:
        return set()
    rows = session.execute(
        text(
            "SELECT npc_id FROM npc_list WHERE LOWER(npc_name) IN "
            "(SELECT LOWER(npc_name) FROM npc_list WHERE npc_id IN :ids)"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": clean},
    ).fetchall()
    return {int(r[0]) for r in rows}


def resolve_names(session, npc_ids) -> Dict[int, str]:
    """``{npc_id: npc_name}`` for display in the admin surface / reports."""
    clean = list({int(i) for i in npc_ids})
    if not clean:
        return {}
    rows = session.execute(
        text("SELECT npc_id, npc_name FROM npc_list WHERE npc_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": clean},
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}
