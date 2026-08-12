"""TEMPORARY split-source observation: which loot sources arrive with company.

Goal: build the evidence to decide, per NPC/raid, whether we should track
nearby players and run split logic (see splitscan report). Buckets we want:
  - raids delivering authoritative rosters (plugin sends varbit/widget rosters
    for ToB/ToA/CoX),
  - bosses that often have other players nearby,
  - bosses that are effectively always killed alone.

Writers sit on the submission-processing path (drop_processor, pb_processor),
so the status_metrics contract applies: fail-open, no DB access, stdlib-only
module imports, redis imported lazily. Every key carries a TTL — stop calling
the recorders and the whole keyspace evaporates on its own.

Keyspace (all TTL'd _TTL, refreshed on write):
  splitscan:npcs               SET of npc_id strings seen
  splitscan:npc:{npc_id}       HASH of counters (see _record)
  splitscan:samples:{npc_id}   LIST of JSON roster samples, newest first, cap 24
  splitscan:seen:{guid}        marker so one kill's multi-item embeds count once
  splitscan:started            unix ts of first observation (report footer)

Version gate: the plugin only sends ``nearby_players`` since the tracker
landed mid-5.2.5 (commit 84356cc, 2026-02) and omits the field entirely when
nobody is nearby — so on older clients "no field" is indistinguishable from
"solo kill". Submissions from plugin versions < MIN_NEARBY_PV therefore bump
volume counters but are excluded from the capable/solo denominators.
"""

from __future__ import annotations

import json
import time
from typing import Optional

_TTL = 30 * 24 * 3600
_SEEN_TTL = 30 * 60
_SAMPLE_CAP = 24

# First plugin version guaranteed to ship NearbyPlayerTracker. Hub builds
# labelled 5.2.5 may predate the tracker commit, so require >= 5.3.
MIN_NEARBY_PV = (5, 3)

INDEX_KEY = "splitscan:npcs"
STARTED_KEY = "splitscan:started"

BUCKET_RAID = "raid"
BUCKET_ACCOMPANIED = "accompanied"
BUCKET_SOLO = "solo"
BUCKET_MIXED = "mixed"
BUCKET_LOW_DATA = "low_data"

# Sources where the plugin captures an authoritative roster (varcstrings /
# raid sidepanel widget) rather than a world-scan. Mirrors the substring
# matching in the plugin's NearbyPlayerTracker.raidTypeForSource().
_RAID_SUBSTRINGS = ("theatre of blood", "tombs of amascut", "chambers of xeric")


def _conn(r=None):
    """Accept a raw redis.Redis, the RedisClient wrapper, or nothing."""
    if r is not None:
        if hasattr(r, "pipeline"):
            return r
        return getattr(r, "client", None)
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def _hash_key(npc_id) -> str:
    return f"splitscan:npc:{int(npc_id)}"


def _samples_key(npc_id) -> str:
    return f"splitscan:samples:{int(npc_id)}"


def pv_tuple(plugin_version) -> Optional[tuple]:
    """``"5.4.0"`` -> ``(5, 4, 0)``; None on anything unparseable."""
    if plugin_version is None:
        return None
    parts = []
    for chunk in str(plugin_version).strip().split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def pv_capable(plugin_version) -> bool:
    """Whether this plugin version reliably sends ``nearby_players``."""
    pv = pv_tuple(plugin_version)
    return pv is not None and pv >= MIN_NEARBY_PV


def parse_nearby(raw) -> list:
    """Nearby-player names from any shape the plugin/paths deliver.

    Accepts a list, a JSON-array string, or the plugin's comma-separated
    string; a lone "none" is the legacy empty sentinel.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if n and str(n).strip()]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        names = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    names = [str(n).strip() for n in parsed if n and str(n).strip()]
            except Exception:
                names = None
        if names is None:
            names = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
    else:
        return []
    if len(names) == 1 and names[0].lower() == "none":
        return []
    return names


def team_size_count(team_size) -> Optional[int]:
    """Canonical PB team encoding -> member count ("Solo"->1, "11-15"->11)."""
    s = str(team_size if team_size is not None else "").strip()
    if not s:
        return None
    if s.lower() == "solo":
        return 1
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def is_raid_source(npc_name) -> bool:
    name = str(npc_name or "").lower()
    return any(sub in name for sub in _RAID_SUBSTRINGS)


def _histogram_field(nearby_count: int) -> str:
    return f"n{nearby_count}" if nearby_count <= 4 else "n5plus"


def _record(kind_prefix: str, *, npc_id, npc_name, player_name, players,
            plugin_version, guid, r=None, now=None,
            party_size=None, roster_source=None, solo_stripped=False) -> None:
    conn = _conn(r)
    if conn is None or npc_id is None:
        return
    try:
        ts = int(now if now is not None else time.time())
        players = players or []
        capable = pv_capable(plugin_version)

        # One kill can arrive as N item embeds sharing a guid; only the first
        # bumps the kill-level counters. No guid -> count it.
        new_kill = True
        if guid:
            new_kill = bool(conn.set(f"splitscan:seen:{guid}", 1, nx=True, ex=_SEEN_TTL))

        hkey = _hash_key(npc_id)
        pipe = conn.pipeline(transaction=False)
        pipe.sadd(INDEX_KEY, str(int(npc_id)))
        pipe.expire(INDEX_KEY, _TTL)
        pipe.set(STARTED_KEY, ts, nx=True)
        pipe.expire(STARTED_KEY, _TTL)
        pipe.hset(hkey, mapping={"name": str(npc_name or ""), "last_seen": ts})
        pipe.hincrby(hkey, f"{kind_prefix}drops", 1)
        if new_kill:
            pipe.hincrby(hkey, f"{kind_prefix}kills", 1)
            if capable:
                pipe.hincrby(hkey, f"{kind_prefix}capable", 1)
                if players:
                    pipe.hincrby(hkey, f"{kind_prefix}with_players", 1)
                    pipe.hincrby(hkey, f"{kind_prefix}players_sum", len(players))
                    pipe.hincrby(hkey, f"{kind_prefix}{_histogram_field(len(players))}", 1)
        # Raid-party evidence (plugin >= 5.4.3): how often the processor's
        # solo gate fires, so "the bug can't recur" is verifiable in prod.
        if new_kill and solo_stripped:
            pipe.hincrby(hkey, f"{kind_prefix}solo_stripped", 1)
        pipe.expire(hkey, _TTL)
        if new_kill and capable and players:
            skey = _samples_key(npc_id)
            sample_data = {"p": str(player_name or ""), "n": players[:20],
                           "k": kind_prefix or "drop", "t": ts}
            if party_size is not None:
                sample_data["ps"] = int(party_size)
            if roster_source:
                sample_data["rs"] = str(roster_source)
            sample = json.dumps(sample_data, separators=(",", ":"))
            pipe.lpush(skey, sample)
            pipe.ltrim(skey, 0, _SAMPLE_CAP - 1)
            pipe.expire(skey, _TTL)
        pipe.execute()
    except Exception:
        pass


def record_drop(*, npc_id, npc_name, player_name, players, plugin_version,
                guid, r=None, now=None,
                party_size=None, roster_source=None, solo_stripped=False) -> None:
    """Observe an accepted plugin drop. ``players`` = normalized nearby list
    (post-gate); ``party_size``/``roster_source`` = the plugin's raid-party
    evidence when sent; ``solo_stripped`` = the processor's solo gate removed
    claimed participants from this submission."""
    _record("", npc_id=npc_id, npc_name=npc_name, player_name=player_name,
            players=players, plugin_version=plugin_version, guid=guid, r=r, now=now,
            party_size=party_size, roster_source=roster_source,
            solo_stripped=solo_stripped)


def record_kill_time(*, npc_id, npc_name, player_name, raw_nearby, team_size,
                     plugin_version, guid, r=None, now=None) -> None:
    """Observe a kill-time submission (rosters ride along for ToB/ToA/CoX).

    Also banks the game-reported ``team_size`` as independent team evidence —
    it exists even where the roster does not.
    """
    conn = _conn(r)
    if conn is None or npc_id is None:
        return
    _record("kt_", npc_id=npc_id, npc_name=npc_name, player_name=player_name,
            players=parse_nearby(raw_nearby), plugin_version=plugin_version,
            guid=guid, r=r, now=now)
    try:
        n = team_size_count(team_size)
        if n is not None:
            hkey = _hash_key(npc_id)
            pipe = conn.pipeline(transaction=False)
            pipe.hincrby(hkey, "kt_team_reported", 1)
            pipe.hincrby(hkey, "kt_team_sum", n)
            if n > 1:
                pipe.hincrby(hkey, "kt_team_gt1", 1)
            pipe.execute()
    except Exception:
        pass


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def snapshot(r=None) -> dict:
    """Everything the report needs: {"started": ts|None, "npcs": {id: stats}}."""
    conn = _conn(r)
    if conn is None:
        return {"started": None, "npcs": {}}
    try:
        ids = sorted(int(m) for m in conn.smembers(INDEX_KEY))
        started_raw = conn.get(STARTED_KEY)
        started = _to_int(started_raw) or None
        npcs = {}
        if ids:
            pipe = conn.pipeline(transaction=False)
            for npc_id in ids:
                pipe.hgetall(_hash_key(npc_id))
            for npc_id, raw in zip(ids, pipe.execute()):
                stats = {}
                for k, v in (raw or {}).items():
                    key = k.decode() if isinstance(k, bytes) else str(k)
                    val = v.decode() if isinstance(v, bytes) else v
                    stats[key] = val if key == "name" else _to_int(val)
                stats.setdefault("name", "")
                npcs[npc_id] = stats
        return {"started": started, "npcs": npcs}
    except Exception:
        return {"started": None, "npcs": {}}


def get_samples(npc_id, limit: int = 5, r=None) -> list:
    conn = _conn(r)
    if conn is None:
        return []
    try:
        out = []
        for raw in conn.lrange(_samples_key(npc_id), 0, max(0, limit - 1)):
            try:
                text = raw.decode() if isinstance(raw, bytes) else raw
                out.append(json.loads(text))
            except Exception:
                continue
        return out
    except Exception:
        return []


def classify(stats: dict, *, min_kills: int = 20, nearby_min: float = 0.10,
             solo_max: float = 0.02) -> tuple:
    """(bucket, nearby_rate|None) for one snapshot row.

    The rate denominator is kills from nearby-capable plugin versions across
    both the drop and kill-time paths; raids are bucketed by name regardless
    of counts since their rosters are authoritative.
    """
    capable = stats.get("capable", 0) + stats.get("kt_capable", 0)
    with_players = stats.get("with_players", 0) + stats.get("kt_with_players", 0)
    rate = (with_players / capable) if capable else None
    if is_raid_source(stats.get("name", "")):
        return BUCKET_RAID, rate
    if capable < min_kills:
        return BUCKET_LOW_DATA, rate
    if rate >= nearby_min:
        return BUCKET_ACCOMPANIED, rate
    if rate <= solo_max:
        return BUCKET_SOLO, rate
    return BUCKET_MIXED, rate
