"""Global personal-best (PB) NPC blocklist.

Some NPCs are reported by the RuneLite plugin with a "kill time" that is not a
real personal best — the game exposes no PB for them, so our tracking is bugged
and produces junk rows. This module holds a **runtime-configurable** set of
``npc_id`` values for which PB submissions are hard-blocked at intake and for
which existing rows are purged.

Storage (durable, no migration): a single global-group config row —
``GroupConfiguration(group_id=2, config_key='blocked_pb_npc_ids')`` — with the
authoritative JSON id-list in ``long_value`` and a short human summary in
``config_value``. This reuses the established "global config on group 2"
precedent (see ``utils/github.py``).

Reads are served from a per-process TTL cache (same shape as
``utils/group_config.py``) so the intake API / bots / queue consumer pick up
admin edits within ``BLOCKLIST_TTL`` seconds without a redeploy. Writes happen
in the web API superadmin surface and invalidate that process's cache
immediately; other processes converge on the next TTL expiry.
"""
from __future__ import annotations

import json
import time
from threading import Lock
from typing import Iterable, Optional, Set

from sqlalchemy import bindparam, text

# The "global group" pseudo-clan that already backs site-wide config values.
GLOBAL_GROUP_ID = 2
CONFIG_KEY = "blocked_pb_npc_ids"
BLOCKLIST_TTL = 30.0  # seconds; matches utils.group_config._TTL

_lock = Lock()
_cache: Optional[Set[int]] = None
_expires: float = 0.0


# --------------------------------------------------------------------------- #
# Parsing / DB access
# --------------------------------------------------------------------------- #
def _parse(raw) -> Set[int]:
    """Parse a stored id-list to a set of ints. JSON array preferred; a bare
    comma-separated string (e.g. hand-edited in the data browser) is tolerated."""
    if not raw:
        return set()
    s = str(raw).strip()
    if not s:
        return set()
    out: Set[int] = set()
    try:
        data = json.loads(s)
        if isinstance(data, list):
            for v in data:
                try:
                    out.add(int(v))
                except (TypeError, ValueError):
                    continue
            return out
    except (ValueError, TypeError):
        pass
    for part in s.strip("[]").split(","):
        part = part.strip().strip('"').strip("'")
        if part:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def _read_row(session):
    from db.models import GroupConfiguration

    return (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == GLOBAL_GROUP_ID,
            GroupConfiguration.config_key == CONFIG_KEY,
        )
        .first()
    )


def _load_from_db(session) -> Set[int]:
    """Authoritative read straight from the DB (bypasses the TTL cache)."""
    row = _read_row(session)
    if not row:
        return set()
    # long_value holds the full list; config_value is only a short summary, but
    # fall back to it in case the row was seeded/edited the other way round.
    raw = row.long_value if (row.long_value and str(row.long_value).strip()) else row.config_value
    return _parse(raw)


def _fresh_session():
    from db import Session

    return Session()


# --------------------------------------------------------------------------- #
# Read path (hot: called per PB submission)
# --------------------------------------------------------------------------- #
def get_blocked_ids(session=None) -> Set[int]:
    """Return the blocked ``npc_id`` set, served from a per-process TTL cache.

    Fails open: any read error yields the last-known set (or empty), so a
    transient DB blip never takes down PB intake.
    """
    global _cache, _expires
    now = time.monotonic()
    with _lock:
        if _cache is not None and now < _expires:
            return set(_cache)

    own = session is None
    s = _fresh_session() if own else session
    try:
        ids = _load_from_db(s)
    except Exception:
        with _lock:
            return set(_cache) if _cache is not None else set()
    finally:
        if own:
            s.close()

    with _lock:
        _cache = set(ids)
        _expires = now + BLOCKLIST_TTL
        return set(_cache)


def is_blocked(npc_id, session=None) -> bool:
    """True if PB submissions for ``npc_id`` are hard-blocked."""
    if npc_id is None:
        return False
    try:
        npc_id = int(npc_id)
    except (TypeError, ValueError):
        return False
    return npc_id in get_blocked_ids(session)


def invalidate() -> None:
    """Drop this process's cached copy (called after a local write)."""
    global _cache, _expires
    with _lock:
        _cache = None
        _expires = 0.0


# --------------------------------------------------------------------------- #
# Write path (superadmin surface + one-off seed)
# --------------------------------------------------------------------------- #
def _apply(session, ids: Set[int]) -> None:
    """Upsert the config row from ``ids`` WITHOUT committing (caller commits)."""
    from db.models import GroupConfiguration

    ordered = sorted(int(i) for i in ids)
    payload = json.dumps(ordered)
    summary = f"{len(ordered)} blocked NPC(s)"
    row = _read_row(session)
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


def _delete_pb_rows(session, ids) -> int:
    """Delete every ``personal_best`` row for ``ids`` (and any ``notified`` rows
    referencing them, to respect the ``notified.pb_id`` FK). No commit; returns
    the number of personal_best rows removed."""
    clean = list({int(i) for i in ids})
    if not clean:
        return 0
    # Clear FK referents first (notified_ibfk_4). None exist for the initial
    # bosses, but adds via the UI must stay correct if that ever changes.
    session.execute(
        text(
            "DELETE FROM notified WHERE pb_id IN "
            "(SELECT id FROM personal_best WHERE npc_id IN :ids)"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": clean},
    )
    res = session.execute(
        text("DELETE FROM personal_best WHERE npc_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": clean},
    )
    return int(res.rowcount or 0)


def pb_entry_count(session, ids) -> int:
    """Count existing ``personal_best`` rows for ``ids`` (UI/dry-run helper)."""
    clean = list({int(i) for i in ids})
    if not clean:
        return 0
    return int(
        session.execute(
            text("SELECT COUNT(*) FROM personal_best WHERE npc_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": clean},
        ).scalar()
        or 0
    )


def sibling_ids(session, npc_ids) -> Set[int]:
    """Expand ``npc_ids`` to every ``npc_list`` id sharing a (case-insensitive)
    ``npc_name`` with any of them, so blocking a boss covers its variant ids
    (e.g. "Giant Mole" -> {5779, 6499})."""
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


def block_only(session, npc_ids: Iterable[int]) -> Set[int]:
    """Add ``npc_ids`` to the blocklist WITHOUT deleting any PB rows, and return
    the full blocked set. Used to seed enforcement ahead of a separate purge
    (so the hard-block goes live on the next service restart before rows are
    deleted, avoiding a regeneration window). Idempotent."""
    updated = _load_from_db(session) | {int(i) for i in npc_ids}
    _apply(session, updated)
    session.commit()
    invalidate()
    return updated


def block_and_purge(session, npc_ids: Iterable[int]) -> dict:
    """Add ``npc_ids`` to the blocklist AND delete their existing PB rows, in a
    single transaction. Returns ``{blocked_ids, added_ids, deleted_pb}``."""
    add = {int(i) for i in npc_ids}
    current = _load_from_db(session)
    updated = current | add
    _apply(session, updated)
    deleted = _delete_pb_rows(session, add)
    session.commit()
    invalidate()
    return {
        "blocked_ids": sorted(updated),
        "added_ids": sorted(add - current),
        "deleted_pb": deleted,
    }


def unblock(session, npc_ids: Iterable[int]) -> dict:
    """Remove ``npc_ids`` from the blocklist. Existing rows are NOT restored
    (they were already purged). Returns ``{blocked_ids, removed_ids}``."""
    remove = {int(i) for i in npc_ids}
    current = _load_from_db(session)
    updated = current - remove
    _apply(session, updated)
    session.commit()
    invalidate()
    return {
        "blocked_ids": sorted(updated),
        "removed_ids": sorted(current & remove),
    }
