"""DB-backed catalog for the event task generator (services/task_generator.py).

Turns the curated ``ENCOUNTERS`` list into the assembled rows the pure
generator sizes and picks from:

- NPC names are checked against ``npc_list`` (a renamed boss drops out
  rather than producing tasks that 422 on save);
- kills/hour: WOM's EHB rate, then our ``npc_ehb_rates``, then the curated
  fallback (the last two flag the row ``kph_estimated``);
- uniques + pet from the Clan Log catalog (``clan_log_sections/items``), with
  per-item rates from the wiki drop tables (``xenforo.dt_npc_loot``,
  ``rarity × rolls``);
- the combat-achievement tiers each boss actually has (``utils.ca_tasks``).

The whole catalog is a few small batched queries and changes weekly at most,
so it is cached in-process for 15 minutes. Clan activity ("who kills what")
is cached per group for 30 minutes.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from sqlalchemy import bindparam, text

log = logging.getLogger(__name__)

_CATALOG_TTL_SECONDS = 900.0
_ACTIVITY_TTL_SECONDS = 1800.0
#: Clans bigger than this (the global group) skip the activity read: the
#: signal is meaningless there and the member join is the expensive part.
ACTIVITY_MAX_MEMBERS = 5000
ACTIVITY_WINDOW_DAYS = 90
MAX_WIKI_RATE = 0.25

_catalog_cache: dict = {"ts": 0.0, "rows": None}
_activity_cache: dict[int, tuple[float, dict, int]] = {}


def _generator():
    # Lazy: the pytest conftest stubs ``services``.
    from services import task_generator
    return task_generator


def invalidate_cache() -> None:
    _catalog_cache["ts"] = 0.0
    _catalog_cache["rows"] = None
    _library_cache["ts"] = 0.0
    _library_cache["rows"] = None
    _activity_cache.clear()


def _wom_rates() -> dict:
    try:
        from utils.wiseoldman import get_ehb_rates_sync

        return get_ehb_rates_sync() or {}
    except Exception:
        return {}


def _expanding(sql: str, *names: str):
    return text(sql).bindparams(*(bindparam(n, expanding=True) for n in names))


def _json_keys(raw) -> list[str]:
    try:
        keys = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(k) for k in keys if k] if isinstance(keys, list) else []


def _sections_for(enc: dict, slugs_by_npc_key: dict[str, list[str]]) -> list[str]:
    """Clan Log section slugs for one encounter: every section whose
    ``npc_keys`` meet the boss's own match keys (plus ``clog_keys``), then any
    slug the encounter names outright. Order-preserving, no repeats."""
    from utils.npc_names import npc_match_key

    keys = [npc_match_key(n) for n in enc["kc_npcs"]] + list(enc.get("clog_keys") or [])
    out: list[str] = []
    for key in keys:
        for slug in slugs_by_npc_key.get(key, []):
            if slug not in out:
                out.append(slug)
    for slug in enc.get("sections") or []:
        if slug not in out:
            out.append(slug)
    return out


def assemble_catalog(s) -> list[dict]:
    """Build the catalog rows (uncached). See the module docstring."""
    tg = _generator()
    encounters = tg.ENCOUNTERS

    all_npcs = sorted({n for e in encounters for n in e["kc_npcs"]})
    npc_ids: dict[str, list[int]] = {}
    for nid, name in s.execute(
        _expanding("SELECT npc_id, npc_name FROM npc_list WHERE npc_name IN :names", "names"),
        {"names": all_npcs},
    ):
        npc_ids.setdefault(str(name), []).append(int(nid))

    # Clan Log sections → (uniques, pets), in the catalog's own order. The
    # whole enabled catalog is a few hundred rows; each encounter claims the
    # sections whose npc_keys meet its own (see ENCOUNTERS).
    section_items: dict[str, list[tuple[str, bool]]] = {}
    slugs_by_npc_key: dict[str, list[str]] = {}
    for slug, npc_keys, name, attributable in s.execute(text(
            "SELECT c.slug, c.npc_keys, i.item_name, i.attributable FROM clan_log_items i "
            "JOIN clan_log_sections c ON c.id = i.section_id "
            "WHERE c.enabled = 1 AND i.enabled = 1 "
            "ORDER BY c.sort_order, i.sort_order, i.id")):
        slug = str(slug)
        if slug not in section_items:
            for key in _json_keys(npc_keys):
                slugs_by_npc_key.setdefault(key, []).append(slug)
        section_items.setdefault(slug, []).append((str(name), bool(attributable)))
    encounter_sections = {e["key"]: _sections_for(e, slugs_by_npc_key) for e in encounters}

    # Canonical item spellings (the task validator requires exact names).
    wanted_items = {n for items in section_items.values() for n, _a in items}
    wanted_items |= {u for e in encounters for u in e.get("uniques") or []}
    canonical: dict[str, str] = {}
    if wanted_items:
        for (name,) in s.execute(
            _expanding("SELECT DISTINCT item_name FROM items WHERE item_name IN :names",
                       "names"),
            {"names": sorted(wanted_items)},
        ):
            canonical[str(name).lower()] = str(name)

    # Wiki per-kill rates: best (rarity × rolls) per (npc, item).
    ids_flat = sorted({i for ids in npc_ids.values() for i in ids})
    wiki: dict[tuple[int, str], float] = {}
    if ids_flat and canonical:
        for nid, name, rate in s.execute(
            _expanding(
                "SELECT l.npc_id, i.item_name, MAX(l.rarity * GREATEST(l.rolls, 1)) "
                "FROM xenforo.dt_npc_loot l JOIN items i ON i.item_id = l.item_id "
                "WHERE l.npc_id IN :ids AND i.item_name IN :names AND l.rarity > 0 "
                "GROUP BY l.npc_id, i.item_name", "ids", "names"),
            {"ids": ids_flat, "names": sorted(canonical.values())},
        ):
            # A unique above 1/4 per kill is a sub-table row read as a whole
            # table (Yama's Oathplate shows as "always"), not a real rate.
            if rate and float(rate) <= MAX_WIKI_RATE:
                wiki[(int(nid), str(name).lower())] = float(rate)

    # Our derived kill rates (bosses WOM doesn't price).
    derived: dict[int, float] = {}
    try:
        for nid, rate in s.execute(text("SELECT npc_id, rate_kph FROM npc_ehb_rates")):
            if rate and float(rate) > 0:
                derived[int(nid)] = float(rate)
    except Exception:
        derived = {}

    # Combat achievement tiers per registry monster.
    ca_tiers_by_monster: dict[str, set[str]] = {}
    ca_display: dict[str, str] = {}
    try:
        from utils.ca_tasks import CA_TIERS, catalog_records

        for rec in catalog_records(s):
            m = " ".join(str(rec.get("monster") or "").lower().split())
            tier = str(rec.get("tier") or "").strip().title()
            if m and tier in CA_TIERS:
                ca_tiers_by_monster.setdefault(m, set()).add(tier)
                ca_display.setdefault(m, str(rec.get("monster")).strip())
        tier_order = list(CA_TIERS)
    except Exception:
        tier_order = []

    try:
        from utils.osrs_pets import canonical_pet_name
    except Exception:  # pragma: no cover - utils always importable in prod
        canonical_pet_name = lambda n: n  # noqa: E731

    rates = _wom_rates()
    rows: list[dict] = []
    for enc in encounters:
        kc_npcs = [n for n in enc["kc_npcs"] if n in npc_ids]
        if not kc_npcs:
            continue
        ids = [i for n in kc_npcs for i in npc_ids[n]]

        kph = float(rates.get(enc.get("wom_metric") or "") or 0)
        estimated = False
        if kph <= 0:
            kph = max((derived.get(i, 0.0) for i in ids), default=0.0)
            estimated = True
        if kph <= 0:
            kph = float(enc.get("kph") or 0)
        if kph <= 0:
            continue

        names: list[str] = []
        pets: list[str] = []
        for slug in encounter_sections[enc["key"]]:
            for name, attributable in section_items.get(slug, []):
                (names if attributable else pets).append(name)
        names += list(enc.get("uniques") or [])
        uniques: list[dict] = []
        seen: set[str] = set()
        for raw in names:
            name = canonical.get(raw.lower())
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            rate = max((wiki.get((i, name.lower()), 0.0) for i in ids), default=0.0)
            uniques.append({"name": name, "rate": rate or None})

        unique_rate = enc.get("unique_rate")
        unique_estimated = unique_rate is not None
        if unique_rate is None:
            rated = [u["rate"] for u in uniques if u["rate"]]
            # A table where most uniques are unrated would under-state the odds.
            if rated and len(rated) * 2 >= len(uniques):
                unique_rate = min(0.5, sum(rated))

        pet_name = enc.get("pet") or (pets[0] if pets else None)
        pet_name = canonical_pet_name(pet_name) if pet_name else None
        pet_rate = None
        if pet_name:
            # The curated figure wins: raid pet rows on the wiki are
            # conditional on a purple, which reads as 1/53 for an Olmlet.
            pet_rate = enc.get("pet_rate") or max(
                (wiki.get((i, pet_name.lower()), 0.0) for i in ids), default=0.0) or None

        monsters = enc.get("ca_monsters") or [enc["label"], *kc_npcs]
        resolved, tiers = [], set()
        for m in monsters:
            key = " ".join(m.lower().split())
            if key in ca_tiers_by_monster:
                resolved.append(ca_display[key])
                tiers |= ca_tiers_by_monster[key]

        rows.append({
            "key": enc["key"],
            "label": enc["label"],
            "short": enc.get("short"),
            "category": enc["category"],
            "unit": enc.get("unit") or "kills",
            "kc_npcs": kc_npcs,
            "npc_ids": ids,
            "kph": kph,
            "kph_estimated": estimated,
            "uniques": uniques,
            "unique_rate": unique_rate,
            "unique_rate_estimated": unique_estimated,
            "conditional": bool(enc.get("conditional")),
            "pet": pet_name,
            "pet_rate": pet_rate,
            "ca_monsters_resolved": resolved,
            "ca_tiers": [t for t in tier_order if t in tiers],
        })
    # A boss with no Clan Log section offers KC and CA tasks only. One or two
    # is normal (Fortis has no attributable drop); many means the catalog and
    # ENCOUNTERS have drifted apart again.
    bare = [e["key"] for e in encounters
            if not encounter_sections[e["key"]] and not e.get("uniques")]
    if bare:
        log.warning("task generator: %d encounter(s) match no Clan Log section: %s",
                    len(bare), ", ".join(bare))
    return rows


def load_catalog(s, *, fresh: bool = False) -> list[dict]:
    """The assembled catalog, cached in-process (see module docstring)."""
    now = time.monotonic()
    if (not fresh and _catalog_cache["rows"] is not None
            and now - _catalog_cache["ts"] < _CATALOG_TTL_SECONDS):
        return _catalog_cache["rows"]
    rows = assemble_catalog(s)
    if rows:  # never cache an empty read (a mid-migration DB)
        _catalog_cache["rows"] = rows
        _catalog_cache["ts"] = now
    return rows


#: Library task types the generator can size (services/task_generator.py
#: library_candidates); the rest (loot sweeps, PBs, custom) are never read.
LIBRARY_TYPES = ("item_collection", "kc_target", "pet_collection", "xp_target", "loot_value")

_library_cache: dict = {"ts": 0.0, "rows": None}


def _library_rows(s, where: str, params: dict) -> list[dict]:
    rows = []
    for lid, name, ttype, target, tv, config in s.execute(
        _expanding(
            "SELECT id, name, type, target, target_value, config "
            "FROM web_event_task_library "
            f"WHERE active = 1 AND type IN :types AND {where} ORDER BY id", "types"),
        {"types": list(LIBRARY_TYPES), **params},
    ):
        try:
            cfg = json.loads(config) if config else None
        except (TypeError, ValueError):
            continue
        if cfg is not None and not isinstance(cfg, dict):
            continue
        rows.append({"id": int(lid), "name": name, "type": ttype, "target": target,
                     "target_value": int(tv) if tv is not None else None, "config": cfg})
    return rows


def load_library(s, group_id: Optional[int]) -> list[dict]:
    """Task-library rows a clan's generator may draw on: every public row
    (cached with the catalog's TTL) plus the clan's own private rows. A read
    failure yields no library rows; the generator works without them."""
    now = time.monotonic()
    try:
        if (_library_cache["rows"] is None
                or now - _library_cache["ts"] >= _CATALOG_TTL_SECONDS):
            _library_cache["rows"] = _library_rows(s, "visibility = 'public'", {})
            _library_cache["ts"] = now
        rows = list(_library_cache["rows"])
        if group_id:
            rows += _library_rows(
                s, "visibility <> 'public' AND group_id = :g", {"g": int(group_id)})
        return rows
    except Exception:
        log.warning("task generator: library read failed", exc_info=True)
        return []


def clan_activity(s, group_id: Optional[int], catalog: list[dict]) -> tuple[dict, int]:
    """``({encounter key: distinct members with drops there}, member count)``
    over the last ``ACTIVITY_WINDOW_DAYS`` days. Empty for no group, the
    global group, or any read failure (the weighting just switches off)."""
    if not group_id:
        return {}, 0
    now = time.monotonic()
    hit = _activity_cache.get(int(group_id))
    if hit and now - hit[0] < _ACTIVITY_TTL_SECONDS:
        return hit[1], hit[2]
    try:
        members = int(s.execute(
            text("SELECT COUNT(*) FROM user_group_association WHERE group_id = :g"),
            {"g": int(group_id)},
        ).scalar() or 0)
        if not members or members > ACTIVITY_MAX_MEMBERS:
            _activity_cache[int(group_id)] = (now, {}, members)
            return {}, members
        npc_to_enc = {i: row["key"] for row in catalog for i in row["npc_ids"]}
        if not npc_to_enc:
            return {}, members
        from datetime import datetime, timedelta

        since = (datetime.utcnow() - timedelta(days=ACTIVITY_WINDOW_DAYS)).strftime("%Y-%m-%d")
        # Driven from the member list through the (player_id, npc_id,
        # date_hour) index — ~0.1s for a 500-member clan.
        players_by_enc: dict[str, set[int]] = {}
        for npc_id, player_id in s.execute(
            _expanding(
                "SET STATEMENT max_statement_time=8 FOR "
                "SELECT DISTINCT h.npc_id, h.player_id FROM user_group_association u "
                "JOIN player_npc_hourly_totals h ON h.player_id = u.player_id "
                " AND h.npc_id IN :ids AND h.date_hour >= :since "
                "WHERE u.group_id = :g", "ids"),
            {"ids": sorted(npc_to_enc), "since": since, "g": int(group_id)},
        ):
            enc = npc_to_enc.get(int(npc_id))
            if enc:
                players_by_enc.setdefault(enc, set()).add(int(player_id))
        activity = {k: len(v) for k, v in players_by_enc.items()}
    except Exception:
        activity, members = {}, 0
    _activity_cache[int(group_id)] = (now, activity, members)
    return activity, members
