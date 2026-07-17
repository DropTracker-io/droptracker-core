"""Deterministically generate item-set event tasks from drop-rate + kill-time data.

No AI involved: a curated CANDIDATES catalog (which OSRS sets exist and how to
frame them) is combined with live DB data to compute expected completion hours
per task, bucket into air/water/earth/fire, scale default_points, and drop
anything whose expected time exceeds --max-hours. Output matches the schema-v2
EventTaskLibraryItem shape (see games/events/task_store/generated_item_sets.pilot.json).

Data sources, in priority order per NPC:
- drop rates: xenforo.dt_npc_loot (rarity * rolls = per-kill probability), with
  per-piece overrides in the catalog for mechanics the table can't express
  (GWD room cycles incl. minions, Barrows reward potential, Moons of Peril
  bad-luck protection, conditional raid rates);
- time per kill: catalog cycle_seconds override, else avg stored PB
  (data.personal_best, milliseconds) * --pb-inflation, else the average gap
  between a player's consecutive distinct drop timestamps (collapses 2x drops
  from one death, gaps clamped 20-300s), else a catalog fallback_cycle.

Run:  cd /store/droptracker/disc && venv/bin/python -m scripts.generate_item_set_tasks --dry-run
      venv/bin/python -m scripts.generate_item_set_tasks --max-hours 15
The gap query scans 90 days of drops for several NPCs; expect ~1-2 minutes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

OUT_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "games", "events", "task_store", "generated_item_sets.json",
)

SOURCE = "generated_v1"

# Difficulty bands as fractions of --max-hours, calibrated so the default 15h
# cap reproduces: air <2h, water 2-5h, earth 5-9h, fire 9-15h.
BAND_FRACTIONS = {"air": 2 / 15, "water": 5 / 15, "earth": 9 / 15, "fire": 1.0}
POINT_FLOORS = {"air": 10, "water": 20, "earth": 30, "fire": 50}
POINT_CEILS = {"air": 15, "water": 28, "earth": 42, "fire": 65}

# ---------------------------------------------------------------------------
# Per-NPC kill-cycle configuration. "pb": use avg personal best * inflation;
# "gap": use inter-drop gaps from data.drops; cycle_seconds: fixed override
# (used where neither source applies, e.g. chest-based content).
# ---------------------------------------------------------------------------
CONTEXTS: dict[str, dict] = {
    "Barrows": {"cycle_seconds": 300},
    "Lunar Chest": {"cycle_seconds": 330},
    "General Graardor": {"gap": True, "fallback_cycle": 150},
    "Kree'arra": {"gap": True, "fallback_cycle": 150},
    "Commander Zilyana": {"gap": True, "fallback_cycle": 150},
    "K'ril Tsutsaroth": {"gap": True, "fallback_cycle": 150},
    "Dagannoth Rex": {"gap": True, "fallback_cycle": 140},
    "Dagannoth Prime": {"gap": True, "fallback_cycle": 140},
    "Dagannoth Supreme": {"gap": True, "fallback_cycle": 140},
    "Zulrah": {"pb": True, "fallback_cycle": 110},
    "Phantom Muspah": {"pb": True, "fallback_cycle": 220},
    "The Nightmare": {"pb": True, "fallback_cycle": 520},
    # Wilderness bosses: no PBs, drop volume too thin for gaps; ~40/hr incl.
    # banking/travel (outside-knowledge estimate).
    "Chaos Fanatic": {"cycle_seconds": 90},
    "Crazy archaeologist": {"cycle_seconds": 90},
    "Scorpia": {"cycle_seconds": 90},
}

# NPCs whose dt_npc_loot rarity is CONDITIONAL on hitting a unique table
# (within-purple odds), not an absolute per-kill rate. Pieces sourced from
# these require an explicit "rate" override or they are skipped with a warning.
CONDITIONAL_RATE_NPCS = {
    "Chambers of Xeric", "Chambers of Xeric Challenge Mode",
    "Tombs of Amascut", "Tombs of Amascut: Expert Mode",
    "Theatre of Blood", "Theatre of Blood: Hard Mode",
    "The Whisperer", "The Leviathan", "Duke Sucellus", "Vardorvis",
}

# ---------------------------------------------------------------------------
# Candidate catalog. Every piece is {"item", "npc"} with optional "rate"
# (per-attempt probability override) and, for kind="exact", "quantity".
# kinds: exact | any_of | assembly | groups | any_path.
# "concurrent": pieces share one attempt stream (co-located NPCs killed in
# rotation, e.g. Dagannoth Kings) instead of being farmed sequentially.
# "expected_attempts" + "attempt_npc": bypass rate math entirely (mechanics
# with pity timers the drop table can't express).
# ---------------------------------------------------------------------------
_BARROWS_PIECES = [
    ("Dharok's helm", "Dharok's platebody", "Dharok's platelegs", "Dharok's greataxe"),
    ("Ahrim's hood", "Ahrim's robetop", "Ahrim's robeskirt", "Ahrim's staff"),
    ("Karil's coif", "Karil's leathertop", "Karil's leatherskirt", "Karil's crossbow"),
    ("Torag's helm", "Torag's platebody", "Torag's platelegs", "Torag's hammers"),
    ("Verac's helm", "Verac's brassard", "Verac's plateskirt", "Verac's flail"),
    ("Guthan's helm", "Guthan's platebody", "Guthan's chainskirt", "Guthan's warspear"),
]
_BARROWS_BROTHERS = ["Dharok", "Ahrim", "Karil", "Torag", "Verac", "Guthan"]
# Specific Barrows piece ~1/408 per roll, 7 rolls at max reward potential
# -> ~1.7% per chest (OSRS Wiki mechanics; dt_npc_loot's 0.5/7 is unusable).
_BARROWS_PIECE_RATE = 1 - (1 - 1 / 408) ** 7
# GWD room cycle: shard chance = boss 1/762 + 3 minions * 1/1524 per cycle.
_GWD_SHARD_RATE = 1 / 762 + 3 / 1524

CANDIDATES: list[dict] = [
    {
        "name": "Barrows Beginnings",
        "description": "Obtain any single Barrows unique from the Barrows chest.",
        "kind": "any_of",
        "pieces": [{"item": it, "npc": "Barrows", "rate": _BARROWS_PIECE_RATE}
                   for brother in _BARROWS_PIECES for it in brother],
        "notes": "Any-unique ~34%/chest at max reward potential (wiki mechanics).",
    },
    *[
        {
            "name": f"{brother} the Collector",
            "description": f"Assemble {brother}'s full Barrows set: "
                           + ", ".join(pieces) + ".",
            "kind": "assembly",
            "pieces": [{"item": it, "npc": "Barrows", "rate": _BARROWS_PIECE_RATE}
                       for it in pieces],
            "notes": "Specific piece ~1.7%/chest; E[max of 4] ~ 122 chests.",
        }
        for brother, pieces in zip(_BARROWS_BROTHERS, _BARROWS_PIECES)
    ],
    {
        "name": "Shard of a God",
        "description": "Obtain any one Godsword shard from the God Wars Dungeon.",
        "kind": "any_of",
        "pieces": [{"item": f"Godsword shard {i}", "npc": "General Graardor",
                    "rate": _GWD_SHARD_RATE} for i in (1, 2, 3)],
        "notes": "Rate models a full room cycle (boss + 3 minions).",
    },
    {
        "name": "Hilt of a God",
        "description": "Obtain any Godsword hilt from a God Wars Dungeon general.",
        "kind": "any_of",
        "pieces": [
            {"item": "Bandos hilt", "npc": "General Graardor"},
            {"item": "Armadyl hilt", "npc": "Kree'arra"},
            {"item": "Zamorak hilt", "npc": "K'ril Tsutsaroth"},
            {"item": "Saradomin hilt", "npc": "Commander Zilyana"},
        ],
    },
    {
        "name": "The Complete Godsword",
        "description": "Obtain all three Godsword shards and any one hilt.",
        "kind": "groups",
        "groups": [
            {"mode": "all_of",
             "pieces": [{"item": f"Godsword shard {i}", "npc": "General Graardor",
                         "rate": _GWD_SHARD_RATE} for i in (1, 2, 3)]},
            {"mode": "any_of",
             "pieces": [
                 {"item": "Bandos hilt", "npc": "General Graardor"},
                 {"item": "Armadyl hilt", "npc": "Kree'arra"},
                 {"item": "Zamorak hilt", "npc": "K'ril Tsutsaroth"},
                 {"item": "Saradomin hilt", "npc": "Commander Zilyana"},
             ]},
        ],
    },
    {
        "name": "Graardor's Wardrobe",
        "description": "Obtain any one piece of Bandos armour from General Graardor.",
        "kind": "any_of",
        "pieces": [{"item": it, "npc": "General Graardor"}
                   for it in ("Bandos chestplate", "Bandos tassets", "Bandos boots")],
    },
    {
        "name": "Bandos Battlegear",
        "description": "Assemble the full Bandos armour set: chestplate, tassets and boots.",
        "kind": "assembly",
        "pieces": [{"item": it, "npc": "General Graardor"}
                   for it in ("Bandos chestplate", "Bandos tassets", "Bandos boots")],
    },
    {
        "name": "Kree's Couture",
        "description": "Obtain any one piece of Armadyl armour from Kree'arra.",
        "kind": "any_of",
        "pieces": [{"item": it, "npc": "Kree'arra"}
                   for it in ("Armadyl helmet", "Armadyl chestplate", "Armadyl chainskirt")],
    },
    {
        "name": "Armadyl Regalia",
        "description": "Assemble the full Armadyl armour set: helmet, chestplate and chainskirt.",
        "kind": "assembly",
        "pieces": [{"item": it, "npc": "Kree'arra"}
                   for it in ("Armadyl helmet", "Armadyl chestplate", "Armadyl chainskirt")],
    },
    {
        "name": "Zulrah's Gift",
        "description": "Obtain any unique from Zulrah: Tanzanite fang, Magic fang, "
                       "Serpentine visage or Uncut onyx.",
        "kind": "any_of",
        "pieces": [{"item": it, "npc": "Zulrah"}
                   for it in ("Tanzanite fang", "Magic fang", "Serpentine visage", "Uncut onyx")],
    },
    {
        "name": "Tanzanite Fang",
        "description": "Obtain a Tanzanite fang from Zulrah.",
        "kind": "exact",
        "pieces": [{"item": "Tanzanite fang", "npc": "Zulrah"}],
    },
    {
        "name": "Toxic Trio",
        "description": "Obtain Zulrah's three weapon uniques: Tanzanite fang, "
                       "Magic fang and Serpentine visage.",
        "kind": "assembly",
        "pieces": [{"item": it, "npc": "Zulrah"}
                   for it in ("Tanzanite fang", "Magic fang", "Serpentine visage")],
    },
    {
        "name": "A Ring for Every Finger",
        "description": "Obtain all four rings from the Dagannoth Kings: "
                       "Berserker, Warrior, Seers and Archers.",
        "kind": "assembly",
        "concurrent": True,
        "pieces": [
            {"item": "Berserker ring", "npc": "Dagannoth Rex"},
            {"item": "Warrior ring", "npc": "Dagannoth Rex"},
            {"item": "Seers ring", "npc": "Dagannoth Prime"},
            {"item": "Archers ring", "npc": "Dagannoth Supreme"},
        ],
        "notes": "Concurrent trio-rotation model; one rotation attempts every ring.",
    },
    {
        "name": "Kingly Jewellery",
        "description": "Obtain any one ring from the Dagannoth Kings.",
        "kind": "any_of",
        "concurrent": True,
        "pieces": [
            {"item": "Berserker ring", "npc": "Dagannoth Rex"},
            {"item": "Warrior ring", "npc": "Dagannoth Rex"},
            {"item": "Seers ring", "npc": "Dagannoth Prime"},
            {"item": "Archers ring", "npc": "Dagannoth Supreme"},
        ],
    },
    {
        "name": "Venator Shard",
        "description": "Obtain a Venator shard from the Phantom Muspah.",
        "kind": "exact",
        "pieces": [{"item": "Venator shard", "npc": "Phantom Muspah"}],
    },
    {
        "name": "Venator Bow",
        "description": "Obtain all five Venator shards from the Phantom Muspah.",
        "kind": "exact",
        "pieces": [{"item": "Venator shard", "npc": "Phantom Muspah", "quantity": 5}],
    },
    {
        "name": "Ancient Icon",
        "description": "Obtain an Ancient icon from the Phantom Muspah.",
        "kind": "exact",
        "pieces": [{"item": "Ancient icon", "npc": "Phantom Muspah"}],
    },
    {
        "name": "Child of the Moons",
        "description": "Assemble one complete Moons of Peril set: Blood moon "
                       "(with Dual macuahuitl), Blue moon (with spear) or "
                       "Eclipse moon (with atlatl).",
        "kind": "any_path",
        "expected_attempts": 55, "attempt_npc": "Lunar Chest",
        "paths": [
            {"label": "Blood moon", "groups": [{"mode": "all_of", "pieces": [
                {"item": it, "npc": "Lunar Chest"} for it in
                ("Blood moon helm", "Blood moon chestplate", "Blood moon tassets",
                 "Dual macuahuitl")]}]},
            {"label": "Blue moon", "groups": [{"mode": "all_of", "pieces": [
                {"item": it, "npc": "Lunar Chest"} for it in
                ("Blue moon helm", "Blue moon chestplate", "Blue moon tassets",
                 "Blue moon spear")]}]},
            {"label": "Eclipse moon", "groups": [{"mode": "all_of", "pieces": [
                {"item": it, "npc": "Lunar Chest"} for it in
                ("Eclipse moon helm", "Eclipse moon chestplate",
                 "Eclipse moon tassets", "Eclipse atlatl")]}]},
        ],
        "notes": "LOW CONFIDENCE: Lunar Chest bad-luck protection weights rolls "
                 "toward unowned pieces; ~55 chests/set is community consensus, "
                 "the table's 1/224/piece would say ~40h.",
    },
    {
        "name": "Full Lunar Wardrobe",
        "description": "Assemble all three complete Moons of Peril sets.",
        "kind": "assembly",
        "expected_attempts": 140, "attempt_npc": "Lunar Chest",
        "pieces": [{"item": it, "npc": "Lunar Chest"} for it in (
            "Blood moon helm", "Blood moon chestplate", "Blood moon tassets",
            "Dual macuahuitl",
            "Blue moon helm", "Blue moon chestplate", "Blue moon tassets",
            "Blue moon spear",
            "Eclipse moon helm", "Eclipse moon chestplate", "Eclipse moon tassets",
            "Eclipse atlatl")],
        "notes": "LOW CONFIDENCE: shared bad-luck protection; ~140 chests estimated.",
    },
    {
        "name": "Shard of the Wilderness",
        "description": "Obtain any Odium or Malediction ward shard from a "
                       "Wilderness boss.",
        "kind": "any_of",
        "pieces": [
            {"item": "Odium shard 1", "npc": "Chaos Fanatic"},
            {"item": "Malediction shard 1", "npc": "Chaos Fanatic"},
            {"item": "Odium shard 2", "npc": "Crazy archaeologist"},
            {"item": "Malediction shard 2", "npc": "Crazy archaeologist"},
            {"item": "Odium shard 3", "npc": "Scorpia"},
            {"item": "Malediction shard 3", "npc": "Scorpia"},
        ],
    },
    {
        "name": "Ward of Odium",
        "description": "Obtain all three Odium ward shards from the Wilderness bosses.",
        "kind": "assembly",
        "pieces": [
            {"item": "Odium shard 1", "npc": "Chaos Fanatic"},
            {"item": "Odium shard 2", "npc": "Crazy archaeologist"},
            {"item": "Odium shard 3", "npc": "Scorpia"},
        ],
    },
    {
        "name": "Inquisitor's Relic",
        "description": "Obtain any piece of Inquisitor's armour from the Nightmare.",
        "kind": "any_of",
        "pieces": [{"item": it, "npc": "The Nightmare"} for it in
                   ("Inquisitor's great helm", "Inquisitor's hauberk",
                    "Inquisitor's plateskirt")],
    },
    {
        "name": "The Full Inquisition",
        "description": "Assemble the complete Inquisitor's set including the mace.",
        "kind": "assembly",
        "pieces": [{"item": it, "npc": "The Nightmare"} for it in
                   ("Inquisitor's great helm", "Inquisitor's hauberk",
                    "Inquisitor's plateskirt", "Inquisitor's mace")],
    },
    {
        "name": "Nightmare Staff",
        "description": "Obtain a Nightmare staff from the Nightmare.",
        "kind": "exact",
        "pieces": [{"item": "Nightmare staff", "npc": "The Nightmare"}],
    },
]


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------
def expected_attempts_all(ps: list[float]) -> float:
    """E[attempts] to hit every one of k independent per-attempt probabilities
    (E[max of k geometrics], exact via inclusion-exclusion)."""
    n = len(ps)
    if n > 18:
        raise ValueError(f"inclusion-exclusion capped at 18 pieces, got {n}")
    total = 0.0
    for mask in range(1, 1 << n):
        prod, bits = 1.0, 0
        for i in range(n):
            if mask >> i & 1:
                prod *= 1.0 - ps[i]
                bits += 1
        total += ((-1) ** (bits + 1)) / (1.0 - prod)
    return total


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------
def _catalog_items(cand: dict) -> list[dict]:
    if cand["kind"] == "groups":
        return [p for g in cand["groups"] for p in g["pieces"]]
    if cand["kind"] == "any_path":
        return [p for path in cand["paths"] for g in path["groups"] for p in g["pieces"]]
    return cand["pieces"]


def resolve_items(session, names: set[str]) -> set[str]:
    """Return the subset of names that exist verbatim in data.items."""
    found: set[str] = set()
    chunk = list(names)
    rows = session.execute(
        text("SELECT DISTINCT item_name FROM data.items WHERE item_name IN :names"),
        {"names": chunk},
    )
    for (name,) in rows:
        found.add(name)
    return found


def fetch_rates(session, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """(npc_name, item_name) -> per-kill probability (rarity * rolls)."""
    out: dict[tuple[str, str], float] = {}
    if not pairs:
        return out
    npcs = sorted({n for n, _ in pairs})
    items = sorted({i for _, i in pairs})
    rows = session.execute(text(
        "SELECT n.npc_name, i.item_name, MAX(l.rarity * l.rolls) "
        "FROM xenforo.dt_npc_loot l "
        "JOIN xenforo.dt_npc n ON n.npc_id = l.npc_id "
        "JOIN data.items i ON i.item_id = l.item_id "
        "WHERE n.npc_name IN :npcs AND i.item_name IN :items "
        "GROUP BY n.npc_name, i.item_name"
    ), {"npcs": npcs, "items": items})
    for npc, item, p in rows:
        if (npc, item) in pairs:
            out[(npc, item)] = float(p)
    return out


def fetch_pb_cycles(session, npcs: list[str], inflation: float,
                    min_samples: int = 50) -> dict[str, float]:
    if not npcs:
        return {}
    rows = session.execute(text(
        "SELECT n.npc_name, COUNT(*), AVG(pb.personal_best) / 1000 "
        "FROM data.personal_best pb "
        "JOIN xenforo.dt_npc n ON n.npc_id = pb.npc_id "
        "WHERE n.npc_name IN :npcs GROUP BY n.npc_name"
    ), {"npcs": npcs})
    return {npc: float(avg) * inflation
            for npc, count, avg in rows if count >= min_samples and avg}


def fetch_gap_cycles(session, npcs: list[str], days: int,
                     min_samples: int = 300) -> dict[str, float]:
    """Avg seconds between a player's consecutive distinct drop timestamps.
    DISTINCT (player_id, date_added) collapses 2x drops from a single death;
    gaps clamped to 20-300s to exclude breaks between sessions."""
    if not npcs:
        return {}
    rows = session.execute(text(
        "WITH k AS ( "
        "  SELECT DISTINCT n.npc_name, d.player_id, d.date_added "
        "  FROM data.drops d JOIN xenforo.dt_npc n ON n.npc_id = d.npc_id "
        "  WHERE n.npc_name IN :npcs "
        "    AND d.date_added > NOW() - INTERVAL :days DAY "
        "), g AS ( "
        "  SELECT npc_name, TIMESTAMPDIFF(SECOND, "
        "    LAG(date_added) OVER (PARTITION BY npc_name, player_id ORDER BY date_added), "
        "    date_added) AS gap "
        "  FROM k) "
        "SELECT npc_name, COUNT(*), AVG(gap) FROM g "
        "WHERE gap BETWEEN 20 AND 300 GROUP BY npc_name"
    ), {"npcs": npcs, "days": days})
    return {npc: float(avg) for npc, count, avg in rows if count >= min_samples and avg}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
class SkipCandidate(Exception):
    pass


def _piece_rate(piece: dict, rates: dict) -> float:
    if "rate" in piece:
        return piece["rate"]
    if piece["npc"] in CONDITIONAL_RATE_NPCS:
        raise SkipCandidate(
            f"{piece['npc']} rarity is conditional on a unique roll; "
            f"'{piece['item']}' needs an explicit rate override")
    p = rates.get((piece["npc"], piece["item"]))
    if p is None:
        raise SkipCandidate(
            f"no dt_npc_loot rate for {piece['item']} @ {piece['npc']}")
    return p


def _cycle_for(npc: str, cycles: dict[str, float]) -> float:
    if npc not in cycles:
        raise SkipCandidate(f"no kill-cycle estimate for {npc}")
    return cycles[npc]


def _eval_pieces(cand_pieces: list[dict], kind: str, concurrent: bool,
                 rates: dict, cycles: dict) -> tuple[float, str]:
    """Expected hours for a flat piece list under any_of / assembly semantics."""
    if concurrent:
        cycle = sum(_cycle_for(p["npc"], cycles)
                    for p in cand_pieces) / len(cand_pieces)
        ps = [_piece_rate(p, rates) for p in cand_pieces]
        if kind == "any_of":
            attempts = 1.0 / sum(ps)
        else:
            attempts = expected_attempts_all(ps)
        return attempts * cycle / 3600, (
            f"concurrent: {attempts:.0f} rotations x {cycle:.0f}s")
    # Sequential: partition by NPC.
    by_npc: dict[str, list[dict]] = {}
    for p in cand_pieces:
        by_npc.setdefault(p["npc"], []).append(p)
    if kind == "any_of":
        best = None
        for npc, pieces in by_npc.items():
            cycle = _cycle_for(npc, cycles)
            hours = (1.0 / sum(_piece_rate(p, rates) for p in pieces)) * cycle / 3600
            if best is None or hours < best[0]:
                best = (hours, f"best source {npc}: 1/{sum(_piece_rate(p, rates) for p in pieces):.5f} "
                               f"per kill x {cycle:.0f}s")
        return best
    hours, parts = 0.0, []
    for npc, pieces in by_npc.items():
        cycle = _cycle_for(npc, cycles)
        attempts = expected_attempts_all([_piece_rate(p, rates) for p in pieces])
        hours += attempts * cycle / 3600
        parts.append(f"{npc}: {attempts:.0f} kills x {cycle:.0f}s")
    return hours, "; ".join(parts)


def _groups_to_pieces(groups: list[dict], rates: dict) -> list[dict]:
    """Flatten a groups spec: all_of contributes its pieces; any_of collapses
    to one pseudo-piece at its most productive NPC (rates summed per NPC)."""
    out: list[dict] = []
    for g in groups:
        if g["mode"] == "all_of":
            out.extend(g["pieces"])
        else:
            by_npc: dict[str, float] = {}
            for p in g["pieces"]:
                by_npc[p["npc"]] = by_npc.get(p["npc"], 0.0) + _piece_rate(p, rates)
            npc = max(by_npc, key=by_npc.get)
            out.append({"item": f"any_of[{len(g['pieces'])}]", "npc": npc,
                        "rate": by_npc[npc]})
    return out


def eval_candidate(cand: dict, rates: dict, cycles: dict) -> tuple[float, str]:
    """Expected solo hours + a human-readable math note."""
    if "expected_attempts" in cand:
        cycle = _cycle_for(cand["attempt_npc"], cycles)
        hours = cand["expected_attempts"] * cycle / 3600
        return hours, (f"override: {cand['expected_attempts']} attempts "
                       f"x {cycle:.0f}s at {cand['attempt_npc']}")
    kind = cand["kind"]
    concurrent = bool(cand.get("concurrent"))
    if kind == "exact":
        piece = cand["pieces"][0]
        qty = piece.get("quantity", 1)
        p = _piece_rate(piece, rates)
        cycle = _cycle_for(piece["npc"], cycles)
        hours = qty / p * cycle / 3600
        return hours, f"{qty}x 1/{1 / p:.0f} at {piece['npc']} x {cycle:.0f}s"
    if kind in ("any_of", "assembly"):
        return _eval_pieces(cand["pieces"], kind, concurrent, rates, cycles)
    if kind == "groups":
        pieces = _groups_to_pieces(cand["groups"], rates)
        return _eval_pieces(pieces, "assembly", concurrent, rates, cycles)
    if kind == "any_path":
        best = None
        for path in cand["paths"]:
            pieces = _groups_to_pieces(path["groups"], rates)
            hours, note = _eval_pieces(pieces, "assembly", concurrent, rates, cycles)
            if best is None or hours < best[0]:
                best = (hours, f"fastest path '{path.get('label', '?')}': {note}")
        return best
    raise SkipCandidate(f"unknown kind {kind!r}")


# ---------------------------------------------------------------------------
# Task assembly
# ---------------------------------------------------------------------------
def bucket(hours: float, max_hours: float) -> str | None:
    for tier in ("air", "water", "earth", "fire"):
        if hours < BAND_FRACTIONS[tier] * max_hours or (
                tier == "fire" and hours <= max_hours):
            return tier
    return None


def scale_points(tier: str, hours: float, max_hours: float) -> int:
    tiers = list(BAND_FRACTIONS)
    lo = BAND_FRACTIONS[tiers[tiers.index(tier) - 1]] * max_hours if tier != "air" else 0.0
    hi = BAND_FRACTIONS[tier] * max_hours
    frac = min(max((hours - lo) / (hi - lo), 0.0), 1.0)
    return round(POINT_FLOORS[tier] + frac * (POINT_CEILS[tier] - POINT_FLOORS[tier]))


def build_config(cand: dict):
    kind = cand["kind"]
    if kind == "exact":
        return None
    if kind == "any_of":
        return {"kind": "any_of", "need": 1,
                "items": [p["item"] for p in cand["pieces"]]}
    if kind == "assembly":
        return {"kind": "assembly", "items": [p["item"] for p in cand["pieces"]]}
    if kind == "groups":
        return {"kind": "groups", "groups": [
            {"mode": g["mode"], "items": [p["item"] for p in g["pieces"]],
             **({"need": g.get("need", 1)} if g["mode"] == "any_of" else {})}
            for g in cand["groups"]]}
    if kind == "any_path":
        return {"kind": "any_path", "paths": [
            {"label": path.get("label"), "groups": [
                {"mode": g["mode"], "items": [p["item"] for p in g["pieces"]],
                 **({"need": g.get("need", 1)} if g["mode"] == "any_of" else {})}
                for g in path["groups"]]}
            for path in cand["paths"]]}
    raise SkipCandidate(f"unknown kind {kind!r}")


def build_task(cand: dict, tier: str, hours: float, note: str,
               max_hours: float) -> dict:
    kind = cand["kind"]
    target = target_value = None
    if kind == "exact":
        target = cand["pieces"][0]["item"]
        target_value = cand["pieces"][0].get("quantity", 1)
    elif kind == "any_of":
        target_value = 1
    elif kind == "assembly":
        target_value = len(cand["pieces"])
    elif kind == "groups":
        target_value = sum(len(g["pieces"]) if g["mode"] == "all_of"
                           else g.get("need", 1) for g in cand["groups"])
    reasoning = f"~{hours:.1f}h expected ({note})."
    if cand.get("notes"):
        reasoning += " " + cand["notes"]
    return {
        "name": cand["name"],
        "description": cand["description"],
        "type": "item_collection",
        "target": target,
        "target_value": target_value,
        "default_points": scale_points(tier, hours, max_hours),
        "difficulty": tier,
        "config": build_config(cand),
        "source": SOURCE,
        "visibility": "public",
        "_reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def generate(session, target_max_hours: float = 15.0, pb_inflation: float = 1.4,
             gap_days: int = 90) -> tuple[list[dict], dict]:
    """Returns (tasks, meta). meta carries excluded/skipped/unresolved lists."""
    all_pieces = [p for c in CANDIDATES for p in _catalog_items(c)]
    names = {p["item"] for p in all_pieces}
    found = resolve_items(session, names)
    unresolved = sorted(names - found)

    rate_pairs = {(p["npc"], p["item"]) for p in all_pieces
                  if "rate" not in p and p["item"] in found}
    rates = fetch_rates(session, rate_pairs)

    cycles: dict[str, float] = {}
    pb_npcs = [n for n, c in CONTEXTS.items() if c.get("pb")]
    gap_npcs = [n for n, c in CONTEXTS.items() if c.get("gap")]
    cycles.update(fetch_pb_cycles(session, pb_npcs, pb_inflation))
    cycles.update(fetch_gap_cycles(session, gap_npcs, gap_days))
    cycle_sources = {n: ("pb" if n in pb_npcs and n in cycles else
                         "gap" if n in gap_npcs and n in cycles else None)
                     for n in CONTEXTS}
    for npc, ctx in CONTEXTS.items():
        if "cycle_seconds" in ctx:
            cycles[npc] = ctx["cycle_seconds"]
            cycle_sources[npc] = "override"
        elif npc not in cycles and "fallback_cycle" in ctx:
            cycles[npc] = ctx["fallback_cycle"]
            cycle_sources[npc] = "fallback"

    tasks, excluded, skipped = [], [], []
    for cand in CANDIDATES:
        if any(p["item"] in unresolved for p in _catalog_items(cand)):
            skipped.append({"name": cand["name"], "reason": "unresolved item name(s)"})
            continue
        try:
            hours, note = eval_candidate(cand, rates, cycles)
        except SkipCandidate as exc:
            skipped.append({"name": cand["name"], "reason": str(exc)})
            continue
        tier = bucket(hours, target_max_hours)
        if tier is None:
            excluded.append({"name": cand["name"], "est_hours": round(hours, 1),
                             "math": note})
            continue
        tasks.append(build_task(cand, tier, hours, note, target_max_hours))

    meta = {
        "source": SOURCE,
        "generated": date.today().isoformat(),
        "generator": "scripts/generate_item_set_tasks.py",
        "target_max_hours": target_max_hours,
        "difficulty_bands_hours": {
            t: round(BAND_FRACTIONS[t] * target_max_hours, 1) for t in BAND_FRACTIONS},
        "kill_cycles_seconds": {n: round(cycles[n]) for n in sorted(cycles)},
        "kill_cycle_sources": cycle_sources,
        "excluded_over_cap": sorted(excluded, key=lambda e: -e["est_hours"]),
        "skipped": skipped,
        "unresolved_items": unresolved,
    }
    return tasks, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-hours", type=float, default=15.0,
                    help="expected-hours cap; tasks above this are excluded (default 15)")
    ap.add_argument("--pb-inflation", type=float, default=1.4,
                    help="PB -> real cycle multiplier for banking/downtime (default 1.4)")
    ap.add_argument("--gap-days", type=int, default=90,
                    help="lookback window for the inter-drop gap estimator (default 90)")
    ap.add_argument("--out", default=OUT_DEFAULT,
                    help=f"output JSON path (default {os.path.relpath(OUT_DEFAULT)})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report without writing the output file")
    args = ap.parse_args()

    # Dedicated engine: the shared base.py engine caps read_timeout at 30s,
    # which the gap-estimator scan (minutes over 90 days of drops) exceeds.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    from db.models.base import DB_USER, DB_PASS
    engine = create_engine(
        f"mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data",
        poolclass=NullPool,
        connect_args={"connect_timeout": 10, "read_timeout": 600, "charset": "utf8mb4"},
    )
    print("Estimating kill cycles (the drop-gap scan can take a few minutes)...",
          file=sys.stderr)
    with engine.connect() as conn:
        tasks, meta = generate(conn, target_max_hours=args.max_hours,
                               pb_inflation=args.pb_inflation, gap_days=args.gap_days)

    by_tier: dict[str, int] = {}
    for t in tasks:
        by_tier[t["difficulty"]] = by_tier.get(t["difficulty"], 0) + 1
    print(f"Generated {len(tasks)} tasks (cap {args.max_hours}h): "
          + ", ".join(f"{t}={by_tier.get(t, 0)}" for t in BAND_FRACTIONS))
    for t in tasks:
        print(f"  [{t['difficulty']:>5}] {t['default_points']:>3}pts  "
              f"{t['name']}: {t['_reasoning']}")
    if meta["excluded_over_cap"]:
        print(f"\nExcluded over {args.max_hours}h cap:")
        for e in meta["excluded_over_cap"]:
            print(f"  {e['est_hours']:>7.1f}h  {e['name']} ({e['math']})")
    if meta["skipped"]:
        print("\nSkipped:")
        for s in meta["skipped"]:
            print(f"  {s['name']}: {s['reason']}")
    if meta["unresolved_items"]:
        print("\nUnresolved item names: " + ", ".join(meta["unresolved_items"]))

    if args.dry_run:
        print("\n(dry run: nothing written)")
        return 0
    with open(args.out, "w") as f:
        json.dump({"_meta": meta, "tasks": tasks}, f, indent=2)
        f.write("\n")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
