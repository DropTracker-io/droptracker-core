"""Clan Log — "which uniques has this clan obtained, and which are missing".

The board a clan actually wants: every boss's uniques laid out, the ones they
have marked with who pulled it and when, the ones they don't left grey. It is
the question suggestion #112 asked that the recap card could not answer — a
recap shows the year's *best* loot, not the *whole* list.

Three ideas hold this up.

**Drops are the evidence.** A slot counts as obtained because a tracked drop
named that item, not because someone's collection log says so. ``collection``
and ``player_pets`` are folded in as *supplements* — they can add a slot (a pet,
an unlock whose drop predates the group joining) but they can never establish
that one is missing. That ordering is the whole reason the board can be trusted.

**Items, not NPCs, are the key.** Matching a drop to a board slot on ``item_id``
alone avoids the entire NPC-attribution minefield (every Barrows brother credits
to ``Barrows``, the Moons to ``Lunar Chest``) and is what makes the query cheap:
the catalog is ~300 rare items, so ``item_id IN (...)`` reads thousands of rows
where a roster-wide scan reads millions. A section's ``npc_keys`` are kept for
display and for the future bounty export, not for matching, with one
exception: an item that sits in more than one section (godsword shards on all
four generals, the dragon pickaxe on six bosses) is matched per section on the
dropping NPC, or one drop would fill every one of those slots. See
:func:`scope_shared_items`.

**The ledger is monthly; the views are folds.** ``clan_log_firsts`` stores one
row per (group, item, month). A month view is those rows, a year view is the
rows inside it, all-time is all of them — so "which slots did we get in 2026"
and "what have we ever got" are the same code path over the same table, and a
new window is a read change rather than a backfill.

What the board cannot know is stated on it rather than hidden: a slot obtained
before the clan tracked anything is indistinguishable from one never obtained,
so the UI says "not seen by DropTracker", never "never obtained".
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

from db.models.clan_log import (
    CLAN_LOG_SCHEMA_VERSION,
    PERIOD_ALL,
    SCOPE_CLAN_LOG,
    SOURCE_CLOG,
    SOURCE_DROP,
    SOURCE_PET,
)
from db.models.recap import RecapSnapshot

# Same ceiling the recap uses, for the same reason: every roster read expands to
# `player_id IN (...)`, and the synthetic groups (group 2 holds every tracked
# player) would read-timeout rather than return.
MAX_ROSTER = 1500

# How many unlocks the summary card lists.
RECENT_UNLOCKS = 8

# Source precedence when two sources claim the same slot in the same month. A
# drop is evidence; the others are supplements.
_SOURCE_RANK = {SOURCE_DROP: 0, SOURCE_CLOG: 1, SOURCE_PET: 2}


class RosterTooLarge(RuntimeError):
    """Raised instead of issuing a query that would read-timeout."""


# --------------------------------------------------------------------------- #
# Periods
#
# 'all', 'YYYY' and 'YYYY-MM'. All three fit `recap_snapshots.period`.
# --------------------------------------------------------------------------- #
def is_valid_period(period: str) -> bool:
    if period == PERIOD_ALL:
        return True
    if len(period) == 4 and period.isdigit():
        return True
    return (
        len(period) == 7
        and period[4] == "-"
        and period[:4].isdigit()
        and period[5:].isdigit()
        and 1 <= int(period[5:]) <= 12
    )


def period_contains(period: str, month: str) -> bool:
    """Whether a ledger row's month falls inside a period."""
    if period == PERIOD_ALL:
        return True
    if len(period) == 4:
        return month[:4] == period
    return month == period


def current_periods(now: Optional[datetime] = None) -> list[str]:
    """The periods a live board keeps materialised: all-time, this year, this
    month. Past months stay readable — they are computed on demand from the
    ledger, which never changes once a month has closed."""
    now = now or datetime.now()
    return [PERIOD_ALL, f"{now.year:04d}", f"{now.year:04d}-{now.month:02d}"]


def month_of(when: datetime) -> str:
    return f"{when.year:04d}-{when.month:02d}"


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def load_catalog(session) -> dict:
    """The curated list of obtainable slots, shaped for both matching and render.

    Returns ``{"sections": [...], "canonical": {item_id: canonical_item_id},
    "scoped": {item_id: {npc_key: slot_id}}, "version": str}``. ``canonical``
    folds variant ids (charged/uncharged, damaged) onto the slot they
    represent, so a drop of any of them credits the one board cell. ``scoped``
    lists the items that appear in more than one section; every item entry
    carries the ``slot_id`` its ledger rows are keyed by.
    """
    rows = session.execute(
        text(
            "SELECT s.id, s.slug, s.label, s.category, s.npc_keys, s.sort_order, "
            "       i.item_id, i.item_name, i.variant_item_ids, i.attributable, "
            "       i.source_hint, i.sort_order "
            "FROM clan_log_sections s "
            "JOIN clan_log_items i ON i.section_id = s.id AND i.enabled = 1 "
            "WHERE s.enabled = 1 "
            "ORDER BY s.sort_order, s.id, i.sort_order, i.id"
        )
    ).fetchall()

    sections: list[dict] = []
    by_id: dict[int, dict] = {}
    canonical: dict[int, int] = {}
    for (sid, slug, label, category, npc_keys, _so, item_id, item_name,
         variants, attributable, source_hint, _iso) in rows:
        section = by_id.get(int(sid))
        if section is None:
            section = {
                "id": int(sid), "slug": slug, "label": label,
                "category": category or "other",
                "npc_keys": _json_list(npc_keys), "items": [],
            }
            by_id[int(sid)] = section
            sections.append(section)
        item_id = int(item_id)
        section["items"].append({
            "item_id": item_id,
            "name": item_name,
            "attributable": bool(attributable),
            "source": source_hint or SOURCE_DROP,
            "slot_id": item_id,
        })
        canonical[item_id] = item_id
        for variant in _json_list(variants):
            try:
                canonical[int(variant)] = item_id
            except (TypeError, ValueError):
                continue
    return {"sections": sections, "canonical": canonical,
            "scoped": scope_shared_items(sections),
            "version": catalog_version(session)}


def slot_id_for(section_id: int, item_id: int) -> int:
    """Ledger key for one section's copy of a shared item.

    Negative so it can never collide with a real item id, and derived rather
    than stored so the ledger needs no extra column. Item ids are well under
    100000.
    """
    return -(int(section_id) * 100000 + int(item_id))


def scope_shared_items(sections: list[dict]) -> dict[int, dict[str, int]]:
    """Give every item that appears in more than one section a per-section slot.

    Returns ``{item_id: {npc_key: slot_id}}`` and rewrites those items'
    ``slot_id`` in place. A drop of a shared item credits the section whose
    ``npc_keys`` hold the dropping NPC, and nothing when no section does (a
    draconic visage from a frost dragon fills neither Vorkath nor KBD).
    """
    seen: dict[int, int] = defaultdict(int)
    for section in sections:
        for item in section["items"]:
            seen[item["item_id"]] += 1
    scoped: dict[int, dict[str, int]] = {}
    for section in sections:
        for item in section["items"]:
            if seen[item["item_id"]] < 2 or item["source"] == SOURCE_PET:
                continue
            slot = slot_id_for(section["id"], item["item_id"])
            item["slot_id"] = slot
            keys = scoped.setdefault(item["item_id"], {})
            for npc_key in section["npc_keys"]:
                keys.setdefault(npc_key, slot)
    return scoped


def catalog_version(session) -> str:
    """Changes whenever the catalog does — part of the image cache signature so
    an admin edit invalidates every rendered board."""
    row = session.execute(
        text(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM clan_log_items "
            "WHERE enabled = 1"
        )
    ).first()
    sections = session.execute(
        text("SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM clan_log_sections "
             "WHERE enabled = 1")
    ).first()
    raw = f"{row[0]}:{row[1]}:{sections[0]}:{sections[1]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #
def visible_group_player_ids(session, group_id: int) -> list[int]:
    """Group members minus players who opted out of public display.

    Delegates to the recap's resolver so the two features cannot disagree about
    who is in a clan or who has opted out.
    """
    from services.recap import _visible_group_player_ids

    return _visible_group_player_ids(session, group_id)


def roster_hash(player_ids: Iterable[int]) -> str:
    """Detects a roster change. A new member brings history that predates the
    tail cursor, so the group needs a full rebuild rather than a tail scan."""
    joined = ",".join(str(p) for p in sorted(set(int(p) for p in player_ids)))
    return hashlib.sha1(joined.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Evidence collection
# --------------------------------------------------------------------------- #
def _claims_from_drops(session, item_ids: list[int], player_ids: Optional[list[int]],
                       since_drop_id: Optional[int] = None) -> list[dict]:
    """Raw catalog drops for a roster (``player_ids=None``: every player).

    Deliberately not aggregated in SQL: a clan's share is a few thousand rows,
    and folding in Python keeps the exact ``drop_id`` and screenshot of the
    first claim, which a ``MIN(date_added)`` aggregate throws away.

    A full read forces ``ix_drops_item_id``. With ~600 catalog ids the
    optimizer prefers the roster's ``player_id`` index and walks every drop
    those members ever had (4+ minutes for a 286-member clan, against ~50s
    cold for the item index). A tail is left to the planner: its
    ``drop_id > cursor`` range is the cheap path there.
    """
    if not item_ids or (player_ids is not None and not player_ids):
        return []
    index_hint = "" if since_drop_id else " FORCE INDEX (ix_drops_item_id)"
    sql = (
        "SELECT drop_id, item_id, player_id, date_added, image_url, npc_id "
        f"FROM drops{index_hint} "
        "WHERE item_id IN :items AND hidden = 0"
    )
    params = {"items": item_ids}
    binds = [bindparam("items", expanding=True)]
    if player_ids is not None:
        sql += " AND player_id IN :players"
        params["players"] = player_ids
        binds.append(bindparam("players", expanding=True))
    if since_drop_id:
        sql += " AND drop_id > :cursor"
        params["cursor"] = int(since_drop_id)
    rows = session.execute(text(sql).bindparams(*binds), params).fetchall()
    return [
        {"ref_id": int(r[0]), "item_id": int(r[1]), "player_id": int(r[2]),
         "at": r[3], "proof": r[4], "source": SOURCE_DROP,
         "npc_id": int(r[5]) if r[5] is not None else None}
        for r in rows if r[3]
    ]


def _claims_from_collection(session, item_ids: list[int], player_ids: list[int],
                            since_log_id: Optional[int] = None) -> list[dict]:
    """Collection-log unlocks, as a supplement.

    These only ever *add* a slot. The plugin reports an unlock the moment it
    happens, so this catches untradeables whose drop landed before the player
    was tracked, and carries its own screenshot.
    """
    if not item_ids or not player_ids:
        return []
    sql = (
        "SELECT log_id, item_id, player_id, date_added, image_url "
        "FROM collection "
        "WHERE item_id IN :items AND player_id IN :players AND date_added IS NOT NULL"
    )
    params = {"items": item_ids, "players": player_ids}
    if since_log_id:
        sql += " AND log_id > :cursor"
        params["cursor"] = int(since_log_id)
    rows = session.execute(
        text(sql).bindparams(
            bindparam("items", expanding=True), bindparam("players", expanding=True)
        ),
        params,
    ).fetchall()
    return [
        {"ref_id": None, "item_id": int(r[1]), "player_id": int(r[2]),
         "at": r[3], "proof": r[4], "source": SOURCE_CLOG}
        for r in rows if r[3]
    ]


def _claims_from_pets(session, item_ids: list[int], player_ids: list[int],
                      since_id: Optional[int] = None) -> list[dict]:
    """Pets. The one slot type that never arrives as a drop."""
    if not item_ids or not player_ids:
        return []
    sql = (
        "SELECT id, item_id, player_id, date_added FROM player_pets "
        "WHERE item_id IN :items AND player_id IN :players AND date_added IS NOT NULL"
    )
    params = {"items": item_ids, "players": player_ids}
    if since_id:
        sql += " AND id > :cursor"
        params["cursor"] = int(since_id)
    rows = session.execute(
        text(sql).bindparams(
            bindparam("items", expanding=True), bindparam("players", expanding=True)
        ),
        params,
    ).fetchall()
    return [
        {"ref_id": None, "item_id": int(r[1]), "player_id": int(r[2]),
         "at": r[3], "proof": None, "source": SOURCE_PET}
        for r in rows if r[3]
    ]


def attach_npc_keys(session, claims: list[dict], catalog: dict) -> None:
    """Set ``npc_key`` on the drop claims of shared items (see
    :func:`scope_shared_items`). One small ``npc_list`` read for the distinct
    NPCs involved; every other claim is left alone."""
    from utils.npc_names import npc_match_key

    scoped = catalog.get("scoped") or {}
    if not scoped:
        return
    canonical = catalog["canonical"]
    wanted = [c for c in claims
              if c.get("npc_id") is not None and canonical.get(c["item_id"]) in scoped]
    npc_ids = sorted({c["npc_id"] for c in wanted})
    if not npc_ids:
        return
    names = dict(session.execute(
        text("SELECT npc_id, npc_name FROM npc_list WHERE npc_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": npc_ids},
    ).fetchall())
    for claim in wanted:
        name = names.get(claim["npc_id"])
        claim["npc_key"] = npc_match_key(name) if name else None


def fold_claims(claims: list[dict], canonical: dict[int, int],
                scoped: Optional[dict[int, dict[str, int]]] = None,
                ) -> dict[tuple[int, str], dict]:
    """Collapse raw claims into one ledger row per (slot, month).

    The winner of a month is the earliest claim in it; a drop beats a
    supplement at the same instant. Everything else in the month becomes count.
    A shared item's claim lands on the slot of the section that names its NPC.
    Supplements carry no NPC, so they never fill a shared slot.
    """
    scoped = scoped or {}
    out: dict[tuple[int, str], dict] = {}
    players: dict[tuple[int, str], set] = defaultdict(set)
    counts: dict[tuple[int, str], int] = defaultdict(int)
    for claim in claims:
        item_id = canonical.get(int(claim["item_id"]))
        if not item_id:
            continue
        if item_id in scoped:
            item_id = scoped[item_id].get(claim.get("npc_key") or "")
            if not item_id:
                continue
        when = claim["at"]
        if not isinstance(when, datetime):
            continue
        key = (item_id, month_of(when))
        # Counts and contributors are tallied outside the winner so that a
        # later-seen-but-earlier claim taking over the row does not reset them.
        players[key].add(int(claim["player_id"]))
        counts[key] += 1
        current = out.get(key)
        better = (
            current is None
            or when < current["obtained_at"]
            or (when == current["obtained_at"]
                and _SOURCE_RANK[claim["source"]] < _SOURCE_RANK[current["source"]])
        )
        if better:
            out[key] = {
                "item_id": item_id,
                "month": key[1],
                "player_id": int(claim["player_id"]),
                "obtained_at": when,
                "drop_id": claim["ref_id"] if claim["source"] == SOURCE_DROP else None,
                "source": claim["source"],
                "proof_url": _clean_proof(claim.get("proof")),
            }
    for key, row in out.items():
        row["obtained_count"] = counts[key]
        row["player_count"] = len(players[key])
    return out


def _clean_proof(raw) -> Optional[str]:
    """`drops.image_url` is not reliably a URL — some rows hold the on-disk path.

    Same correction ``services/recap._public_image_url`` makes; an unservable
    value is dropped rather than rendered as a broken image.
    """
    if not raw:
        return None
    value = str(raw)
    if value.startswith("http://") or value.startswith("https://"):
        return value[:500]
    marker = "/static/assets/img/"
    if marker in value:
        return ("https://www.droptracker.io/img/" + value.split(marker, 1)[1])[:500]
    return None


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def upsert_ledger(session, group_id: int, folded: dict[tuple[int, str], dict]) -> dict:
    """Write folded claims to ``clan_log_firsts``.

    ``obtained_at`` only ever moves earlier: a later sighting of something the
    group already had that month is a count, not a new first. That is what makes
    the incremental tail and a full rebuild converge on the same rows.
    """
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    if not folded:
        return stats
    existing = {
        (int(r[1]), r[2]): r
        for r in session.execute(
            text(
                "SELECT id, item_id, month, player_id, obtained_at, obtained_count, "
                "       player_count, source FROM clan_log_firsts WHERE group_id = :gid"
            ),
            {"gid": group_id},
        ).fetchall()
    }
    for key, row in folded.items():
        current = existing.get(key)
        if current is None:
            session.execute(
                text(
                    "INSERT INTO clan_log_firsts "
                    "(group_id, item_id, month, player_id, obtained_at, drop_id, "
                    " source, proof_url, obtained_count, player_count, updated_at) "
                    "VALUES (:gid, :item_id, :month, :player_id, :obtained_at, :drop_id, "
                    "        :source, :proof_url, :obtained_count, :player_count, NOW()) "
                    "ON DUPLICATE KEY UPDATE "
                    "  obtained_count = obtained_count + VALUES(obtained_count), "
                    "  player_count = GREATEST(player_count, VALUES(player_count))"
                ),
                {"gid": group_id, **row},
            )
            stats["inserted"] += 1
            continue

        _id, _iid, _m, player_id, obtained_at, count, player_count, source = current
        earlier = row["obtained_at"] < obtained_at
        new_count = max(int(count or 0), row["obtained_count"])
        new_players = max(int(player_count or 0), row["player_count"])
        if not earlier and new_count == count and new_players == player_count:
            stats["unchanged"] += 1
            continue
        params = {"id": _id, "count": new_count, "players": new_players}
        sql = ("UPDATE clan_log_firsts SET obtained_count = :count, "
               "player_count = :players, updated_at = NOW()")
        if earlier:
            sql += (", player_id = :player_id, obtained_at = :obtained_at, "
                    "drop_id = :drop_id, source = :source, proof_url = :proof_url")
            params.update({
                "player_id": row["player_id"], "obtained_at": row["obtained_at"],
                "drop_id": row["drop_id"], "source": row["source"],
                "proof_url": row["proof_url"],
            })
        session.execute(text(sql + " WHERE id = :id"), params)
        stats["updated"] += 1
    return stats


def prefetch_drop_claims(session, catalog: dict) -> dict:
    """Every catalog drop for every player, read once, for a many-group rebuild.

    A full rebuild's drop read costs the same whatever the roster: the plan
    walks ``ix_drops_item_id`` across all ~380k catalog rows and filters by
    player afterwards (~50s cold on the 677-slot catalog). Rebuilding ~300
    groups one by one repeats that read ~300 times. ``backfill_group`` reads it
    once here and each :func:`rebuild_group` takes its roster's share.

    ``max_drop_id`` is read BEFORE the scan and rows above it are discarded, so
    a rebuild can tail from it without gaps or double counts.
    """
    ceiling = _max_id(session, "drops", "drop_id")
    claims = [c for c in _claims_from_drops(session, sorted(catalog["canonical"]), None)
              if c["ref_id"] <= ceiling]
    return {"claims": claims, "max_drop_id": ceiling}


def rebuild_group(session, group_id: int, catalog: Optional[dict] = None,
                  player_ids: Optional[list[int]] = None,
                  prefetched: Optional[dict] = None) -> dict:
    """Recompute a group's whole ledger from every source, from the beginning.

    ``prefetched`` (from :func:`prefetch_drop_claims`) replaces the drop read
    with the roster's share of it, plus a tail of anything newer.
    """
    catalog = catalog or load_catalog(session)
    player_ids = player_ids if player_ids is not None else visible_group_player_ids(
        session, group_id
    )
    if len(player_ids) > MAX_ROSTER:
        raise RosterTooLarge(
            f"group {group_id} has {len(player_ids)} visible members (> {MAX_ROSTER})"
        )
    if not player_ids:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "roster": 0}

    item_ids = sorted(catalog["canonical"].keys())
    pet_ids = [
        i["item_id"] for s in catalog["sections"] for i in s["items"]
        if i["source"] == SOURCE_PET
    ]
    if prefetched is not None:
        roster = set(player_ids)
        # Copies: attach_npc_keys writes into claims, and the list is shared.
        claims = [dict(c) for c in prefetched["claims"] if c["player_id"] in roster]
        claims += _claims_from_drops(session, item_ids, player_ids,
                                     prefetched["max_drop_id"])
    else:
        claims = _claims_from_drops(session, item_ids, player_ids)
    claims += _claims_from_collection(session, item_ids, player_ids)
    claims += _claims_from_pets(session, pet_ids or item_ids, player_ids)
    attach_npc_keys(session, claims, catalog)

    stats = upsert_ledger(session, group_id, fold_claims(
        claims, catalog["canonical"], catalog.get("scoped")))
    stats["roster"] = len(player_ids)
    stats["claims"] = len(claims)
    return stats


# --------------------------------------------------------------------------- #
# Board payload
# --------------------------------------------------------------------------- #
def _player_names(session, player_ids: set[int]) -> dict[int, str]:
    if not player_ids:
        return {}
    rows = session.execute(
        text("SELECT player_id, player_name FROM players WHERE player_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": sorted(player_ids)},
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def build_payload(session, group_id: int, period: str,
                  catalog: Optional[dict] = None,
                  cursor: Optional[dict] = None) -> dict:
    """Assemble the board for one group over one period, from the ledger.

    Cheap by construction: one indexed read of ``clan_log_firsts`` plus a name
    lookup. Nothing here touches ``drops``, which is what lets the page and the
    image render from a stored snapshot.
    """
    if not is_valid_period(period):
        raise ValueError(f"bad period: {period!r}")
    catalog = catalog or load_catalog(session)

    rows = session.execute(
        text(
            "SELECT item_id, month, player_id, obtained_at, source, proof_url, "
            "       obtained_count, player_count "
            "FROM clan_log_firsts WHERE group_id = :gid"
        ),
        {"gid": group_id},
    ).fetchall()

    # Fold the monthly rows down to one claim per item for this window.
    claims: dict[int, dict] = {}
    for item_id, month, player_id, obtained_at, source, proof, count, players in rows:
        if not period_contains(period, month):
            continue
        item_id = int(item_id)
        current = claims.get(item_id)
        if current is None:
            claims[item_id] = {
                "player_id": int(player_id), "at": obtained_at, "source": source,
                "proof": proof, "count": int(count or 0),
                "shared": int(players or 1) > 1,
            }
            continue
        current["count"] += int(count or 0)
        if int(players or 1) > 1 or int(player_id) != current["player_id"]:
            # Same rule the recap's annual fold uses: a name survives only
            # while every contributing row agrees it was sole.
            current["shared"] = True
        if obtained_at < current["at"]:
            current.update({
                "player_id": int(player_id), "at": obtained_at,
                "source": source, "proof": proof,
            })

    names = _player_names(session, {c["player_id"] for c in claims.values()})
    group_name = session.execute(
        text("SELECT group_name FROM groups WHERE group_id = :gid"), {"gid": group_id}
    ).scalar()

    sections = []
    per_category: dict[str, dict] = {}
    total = obtained_total = 0
    recent: list[dict] = []
    for section in catalog["sections"]:
        items = []
        section_obtained = 0
        for item in section["items"]:
            claim = claims.get(item.get("slot_id", item["item_id"]))
            entry = {
                "item_id": item["item_id"],
                "name": item["name"],
                "obtained": claim is not None,
                "attributable": item["attributable"],
            }
            if claim:
                section_obtained += 1
                entry.update({
                    "by": names.get(claim["player_id"]),
                    "player_id": claim["player_id"],
                    "at": claim["at"].isoformat(timespec="seconds"),
                    "count": claim["count"],
                    "shared": claim["shared"],
                    "source": claim["source"],
                    "proof": _clean_proof(claim["proof"]),
                })
                recent.append({
                    "item_id": item["item_id"], "name": item["name"],
                    "by": names.get(claim["player_id"]), "at": entry["at"],
                    "section": section["label"],
                })
            items.append(entry)

        total += len(items)
        obtained_total += section_obtained
        category = section["category"]
        bucket = per_category.setdefault(category, {"total": 0, "obtained": 0})
        bucket["total"] += len(items)
        bucket["obtained"] += section_obtained
        sections.append({
            "slug": section["slug"], "label": section["label"],
            "category": category,
            "total": len(items), "obtained": section_obtained,
            "items": items,
        })

    recent.sort(key=lambda r: r["at"], reverse=True)
    payload = {
        "schema_version": CLAN_LOG_SCHEMA_VERSION,
        "catalog_version": catalog["version"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "group_id": group_id,
        "group_name": group_name,
        "period": period,
        "sections": sections,
        "summary": {
            "total": total,
            "obtained": obtained_total,
            "pct": round(100.0 * obtained_total / total, 1) if total else 0.0,
            "per_category": per_category,
        },
        "recent": recent[:RECENT_UNLOCKS],
    }
    if cursor:
        payload["cursor"] = cursor
    return payload


# --------------------------------------------------------------------------- #
# Snapshot persistence (shares `recap_snapshots` under its own scope)
# --------------------------------------------------------------------------- #
def save_board(session, group_id: int, period: str, payload: dict):
    row = (
        session.query(RecapSnapshot)
        .filter(
            RecapSnapshot.scope == SCOPE_CLAN_LOG,
            RecapSnapshot.subject_id == group_id,
            RecapSnapshot.period == period,
        )
        .first()
    )
    blob = json.dumps(payload, separators=(",", ":"), default=str)
    if row:
        row.payload = blob
        row.schema_version = CLAN_LOG_SCHEMA_VERSION
        row.generated_at = datetime.now()
    else:
        row = RecapSnapshot(
            scope=SCOPE_CLAN_LOG, subject_id=group_id, period=period,
            payload=blob, schema_version=CLAN_LOG_SCHEMA_VERSION,
            generated_at=datetime.now(),
        )
        session.add(row)
    return row


def load_board(session, group_id: int, period: str) -> Optional[dict]:
    row = (
        session.query(RecapSnapshot)
        .filter(
            RecapSnapshot.scope == SCOPE_CLAN_LOG,
            RecapSnapshot.subject_id == group_id,
            RecapSnapshot.period == period,
        )
        .first()
    )
    if not row:
        return None
    try:
        return json.loads(row.payload)
    except (TypeError, ValueError):
        return None


def stored_periods(session, group_id: int) -> list[str]:
    """Every period this group has a stored board for, newest first (all-time
    pinned to the front)."""
    rows = session.execute(
        text(
            "SELECT period FROM recap_snapshots WHERE scope = :scope AND subject_id = :gid"
        ),
        {"scope": SCOPE_CLAN_LOG, "gid": group_id},
    ).fetchall()
    periods = {r[0] for r in rows}
    out = [PERIOD_ALL] if PERIOD_ALL in periods else []
    return out + sorted((p for p in periods if p != PERIOD_ALL), reverse=True)


def ledger_periods(session, group_id: int) -> list[str]:
    """Periods the ledger can answer for, whether or not a snapshot exists —
    what the page's period picker offers.

    Empty for a group with no ledger rows, and deliberately so: it is also the
    gate on building a board on demand, and a group that has never been swept
    (or does not exist) must get "no board here" rather than a freshly minted
    one reporting a very believable 0%.
    """
    rows = session.execute(
        text("SELECT DISTINCT month FROM clan_log_firsts WHERE group_id = :gid"),
        {"gid": group_id},
    ).fetchall()
    months = sorted((r[0] for r in rows), reverse=True)
    if not months:
        return []
    years = sorted({m[:4] for m in months}, reverse=True)
    return [PERIOD_ALL] + years + months


# --------------------------------------------------------------------------- #
# Refresh orchestration
# --------------------------------------------------------------------------- #
def refresh_group(session, group_id: int, catalog: Optional[dict] = None,
                  full: bool = False, periods: Optional[list[str]] = None,
                  prefetched: Optional[dict] = None) -> dict:
    """Bring one group's ledger and stored boards up to date.

    A roster change or a catalog change forces the full path: a new member
    brings history the tail cursor already passed, and a new catalog item was
    never scanned for at all.
    """
    catalog = catalog or load_catalog(session)
    player_ids = visible_group_player_ids(session, group_id)
    if not player_ids:
        return {"skipped": "empty roster"}

    stored = load_board(session, group_id, PERIOD_ALL) or {}
    cursor = dict(stored.get("cursor") or {})
    current_roster = roster_hash(player_ids)
    stale = (
        full
        or not stored
        or stored.get("catalog_version") != catalog["version"]
        or cursor.get("roster_hash") != current_roster
    )

    # Watermarks are read BEFORE the scan, never after. The scan is unbounded
    # above (``... WHERE drop_id > :cursor``), so a watermark read afterwards
    # can include rows the scan never saw — the next tail then starts past
    # them and they are lost until a full rebuild. Under REPEATABLE READ both
    # reads shared one transaction snapshot and that could not happen; under
    # READ COMMITTED it can. Same ceiling-first ordering as
    # ``services/item_totals.process_new_drops``. Re-scanning a row on the next
    # cycle is harmless: upsert_ledger folds by earliest-wins and max().
    watermarks = {
        "drop_id": _max_id(session, "drops", "drop_id"),
        "log_id": _max_id(session, "collection", "log_id"),
        "pet_id": _max_id(session, "player_pets", "id"),
    }

    if stale:
        stats = rebuild_group(session, group_id, catalog=catalog, player_ids=player_ids,
                              prefetched=prefetched)
        mode = "rebuild"
    else:
        stats = _tail_group(session, group_id, catalog, player_ids, cursor)
        mode = "tail"

    cursor.update({"roster_hash": current_roster, **watermarks})

    for period in (periods or current_periods()):
        payload = build_payload(session, group_id, period, catalog=catalog,
                                cursor=cursor if period == PERIOD_ALL else None)
        save_board(session, group_id, period, payload)

    return {"mode": mode, **stats}


def _tail_group(session, group_id: int, catalog: dict, player_ids: list[int],
                cursor: dict) -> dict:
    item_ids = sorted(catalog["canonical"].keys())
    pet_ids = [
        i["item_id"] for s in catalog["sections"] for i in s["items"]
        if i["source"] == SOURCE_PET
    ]
    claims = _claims_from_drops(session, item_ids, player_ids, cursor.get("drop_id"))
    claims += _claims_from_collection(session, item_ids, player_ids, cursor.get("log_id"))
    claims += _claims_from_pets(session, pet_ids or item_ids, player_ids,
                               cursor.get("pet_id"))
    attach_npc_keys(session, claims, catalog)
    stats = upsert_ledger(session, group_id, fold_claims(
        claims, catalog["canonical"], catalog.get("scoped")))
    stats["claims"] = len(claims)
    stats["roster"] = len(player_ids)
    return stats


def _max_id(session, table: str, column: str) -> int:
    row = session.execute(text(f"SELECT COALESCE(MAX({column}), 0) FROM {table}")).first()
    return int(row[0] or 0)


def _json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []
