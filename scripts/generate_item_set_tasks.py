"""Deterministically generate item-collection event tasks, game-wide, from
drop-rate + kill-time data. No AI involved.

Pipeline:
1. DISCOVER active NPCs: top --top-npcs by distinct players in data.drops over
   --activity-days (or an explicit --npc list). Activity gating is the main
   "makes sense" filter — tasks are only minted for content players actually
   do, which also guarantees kill-time data exists.
2. MINT candidates per NPC from xenforo.dt_npc_loot (rarity * rolls = per-kill
   probability, clamped to a notability window):
     - exact:    each of the NPC's rarest drops (deduped across NPCs);
     - any_of:   "any unique from X" when the NPC has 2+ notable drops;
     - assembly: item-name families ("Bandos ...", "Inquisitor's ...") with
                 2+ pieces on the same NPC become "assemble the set" tasks.
   NPCs whose stored rarity is conditional on a unique roll (raids, DT2) are
   skipped rather than mis-rated.
3. Overlay CURATED candidates for mechanics the loot table can't express
   (Barrows reward potential, GWD room cycles, Moons of Peril bad-luck
   protection, Dagannoth trio rotation). Curated wins on identical item sets.
4. SCORE each candidate: expected solo hours from drop math (exact
   inclusion-exclusion for "collect all") x per-NPC kill cycle, sourced from
   catalog override, else avg stored PB (data.personal_best, ms) *
   --pb-inflation, else avg gap between a player's consecutive distinct drop
   timestamps (collapses 2x drops from one death), else skipped.
5. FILTER to --max-hours, bucket into air/water/earth/fire (band edges scale
   with the cap), scale default_points, and optionally --count select a
   tier-balanced, NPC-diverse subset.

Output matches the schema-v2 EventTaskLibraryItem shape; _reasoning/_npc/_meta
keys are review aids to strip at import.

Run:  cd /store/droptracker/disc && venv/bin/python -m scripts.generate_item_set_tasks --dry-run
      venv/bin/python -m scripts.generate_item_set_tasks --count 25
      venv/bin/python -m scripts.generate_item_set_tasks --npc Zulrah --npc Vorkath --dry-run
The discovery + drop-gap scans read months of data.drops; expect minutes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
TIER_ORDER = ("air", "water", "earth", "fire")
# Absolute upper edges in expected solo hours. Difficulty is INDEPENDENT of the
# --max-hours exclusion cap: a task's tier never changes when you raise/lower
# the cap, and raising the cap simply lets more hard content through, all of it
# landing in fire (>= earth edge). Fire is open-ended up to the cap.
TIER_EDGES_HOURS = {"air": 2.0, "water": 5.0, "earth": 9.0}  # fire = >= 9h
POINT_FLOORS = {"air": 10, "water": 20, "earth": 30, "fire": 50}
POINT_CEILS = {"air": 15, "water": 28, "earth": 42, "fire": 65}

# Notability window for auto-minted drops: commoner than 1/20 isn't a task,
# rarer than 1/50k is table noise (e.g. 1/1M cross-drop minion rows).
MIN_NOTABLE_P = 1 / 50000
MAX_NOTABLE_P = 1 / 20
# An item is task-worthy if its unit GE value clears --min-value, OR it is
# genuinely rare AND has strictly zero observed value — i.e. untradeable
# (clue scrolls, dark totem pieces, wildy keys). Rare-but-cheap tradeables
# (seeds, key halves, firelighter-grade filler, rare stack-size rows of
# commons) carry a nonzero price and are junk tasks despite their rarity.
RARE_NOTABLE_P = 1 / 50
ITEM_BLACKLIST = {"Coins", "Message"}
# Junk/meta drops that are never a meaningful "obtain X" task even when they
# come from a real boss (clue scrolls & scroll boxes are gated on doing the
# clue, not the kill; caskets/firelighters are filler).
ITEM_BLACKLIST_SUBSTRINGS = ("firelighter", "clue scroll", "scroll box",
                             "reward casket", "champion scroll")
# Tasks quicker than this are filler even for air tier.
MIN_TASK_HOURS = 0.1
# Per-NPC caps keep auto output reviewable.
MAX_EXACT_PER_NPC = 4
MAX_ANY_OF_ITEMS = 20
MAX_FAMILY_PIECES = 8

# ---------------------------------------------------------------------------
# Per-NPC kill-cycle overrides for content where neither PBs nor drop gaps
# give a sane number (chests/instanced runs); plus fallbacks for thin data.
# Anything not listed resolves PB -> gap automatically.
# ---------------------------------------------------------------------------
CONTEXTS: dict[str, dict] = {
    "Barrows": {"cycle_seconds": 300},
    "Lunar Chest": {"cycle_seconds": 330},
    "Chaos Fanatic": {"cycle_seconds": 90},
    "Crazy archaeologist": {"cycle_seconds": 90},
    "Scorpia": {"cycle_seconds": 90},
    "General Graardor": {"fallback_cycle": 150},
    "Kree'arra": {"fallback_cycle": 150},
    "Commander Zilyana": {"fallback_cycle": 150},
    "K'ril Tsutsaroth": {"fallback_cycle": 150},
    "Dagannoth Rex": {"fallback_cycle": 140},
    "Dagannoth Prime": {"fallback_cycle": 140},
    "Dagannoth Supreme": {"fallback_cycle": 140},
}

# NPCs whose dt_npc_loot rarity is CONDITIONAL on hitting a unique table
# (within-purple odds), not an absolute per-kill rate. For these, per-item
# rates are derived EMPIRICALLY from our own drops: distinct
# (player, timestamp) events approximate completions (2x drops from one kill
# share a timestamp), and item events / completions is the true
# per-completion probability observed across the player base.
EMPIRICAL_RATE_NPCS = {
    "Chambers of Xeric", "Chambers of Xeric Challenge Mode",
    "Tombs of Amascut", "Tombs of Amascut: Entry Mode",
    "Tombs of Amascut: Expert Mode",
    "Theatre of Blood", "Theatre of Blood: Entry Mode",
    "Theatre of Blood: Hard Mode",
    "The Whisperer", "The Leviathan", "Duke Sucellus", "Vardorvis",
}
# Minimum observed drops of an item before its empirical rate is trusted.
EMPIRICAL_MIN_EVENTS = 5

# "Sources" that are not kill content: the loot is gated on first obtaining and
# then completing the activity, so the casket/reward drop rate massively
# understates real time-to-obtain (a master clue's 3rd age is not a ~57h task —
# it's hundreds of hours once you count acquiring and running the clues). These
# are dropped from discovery entirely; add more at runtime with --exclude-npc.
NON_KILL_SOURCE_PREFIXES = ("Clue Scroll", "Reward Casket", "Scroll box")

# First words too generic to define an item family (metal/junk prefixes).
# Value/rarity notability runs first, so this only needs to catch words that
# group unrelated NOTABLE items (e.g. two expensive dragon items aren't a set).
FAMILY_STOPWORDS = {
    "rune", "dragon", "black", "white", "adamant", "mithril", "steel", "iron",
    "bronze", "uncut", "grimy", "clue", "crystal", "ancient", "broken",
    "looting", "blighted", "super", "prayer", "magic", "mystic",
}

# ---------------------------------------------------------------------------
# Curated candidates: ONLY mechanics the loot table can't express. Pieces are
# {"item", "npc"} with optional "rate" (per-attempt probability override) and,
# for kind="exact", "quantity". kinds: exact | any_of | assembly | groups |
# any_path. "concurrent": pieces share one attempt stream (co-located NPCs
# killed in rotation). "expected_attempts" + "attempt_npc": bypass rate math
# (pity timers etc.).
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

CURATED: list[dict] = [
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
        "name": "Ward of Odium",
        "description": "Obtain all three Odium ward shards from the Wilderness bosses.",
        "kind": "assembly",
        "pieces": [
            {"item": "Odium shard 1", "npc": "Chaos Fanatic"},
            {"item": "Odium shard 2", "npc": "Crazy archaeologist"},
            {"item": "Odium shard 3", "npc": "Scorpia"},
        ],
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
# Flat-file stats cache
# ---------------------------------------------------------------------------
CACHE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".event_task_stats_cache.json")


class StatsCache:
    """JSON flat-file memo for the expensive drops-table computations (kill
    cycles, empirical rates, discovery, item values). Entries carry the date
    they were computed and expire after ttl_days — drop-rate/kill-time stats
    drift slowly, so re-deriving them on every run is pure waste. A stored
    empty/zero value means "queried before, no data" and is honored so known
    dead ends aren't re-scanned either."""

    def __init__(self, path: str | None, ttl_days: float,
                 read_enabled: bool = True):
        self.path, self.ttl, self.read_enabled = path, ttl_days, read_enabled
        self.data: dict = {}
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except (ValueError, OSError):
                self.data = {}

    def get(self, section: str, key: str):
        """None = not cached (or expired/refreshing); anything else is a hit."""
        if not self.read_enabled:
            return None
        entry = self.data.get(section, {}).get(str(key))
        if not isinstance(entry, dict) or "at" not in entry:
            return None
        try:
            age = (date.today() - date.fromisoformat(entry["at"])).days
        except ValueError:
            return None
        if age > self.ttl:
            return None
        return entry.get("v")

    def put(self, section: str, key: str, value) -> None:
        if self.path is None:
            return
        self.data.setdefault(section, {})[str(key)] = {
            "at": date.today().isoformat(), "v": value}
        self.dirty = True

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
        self.dirty = False


def _split_cached(cache: StatsCache, section: str, keys: list[str]):
    """(hits dict, miss keys) for a batch of cache lookups."""
    hits, misses = {}, []
    for key in keys:
        value = cache.get(section, key)
        if value is None:
            misses.append(key)
        else:
            hits[key] = value
    return hits, misses


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------
def discover_active_npcs(conn, days: int, top: int,
                         cache: StatsCache) -> list[tuple[str, int]]:
    """(npc_name, distinct_players) for the top NPCs by total GP dropped in
    the window. Ranking by value keeps the list boss-shaped — ranking by
    player count surfaces trash mobs everyone kills incidentally (Man, Imp).
    Uses the pre-aggregated player_npc_hourly_totals table (partition =
    YYYYMM) — the same scan over raw data.drops times out."""
    today = date.today()
    months_back = max((days + 29) // 30 - 1, 0)
    year, month = today.year, today.month - months_back
    while month < 1:
        year, month = year - 1, month + 12
    part_lo = year * 100 + month
    cached = cache.get("discovery", f"{part_lo}:{top}")
    if cached is not None:
        return [(name, int(players)) for name, players in cached]
    print(f"Discovering active NPCs (~{days}d window)...", file=sys.stderr)
    rows = conn.execute(text(
        "SELECT n.npc_name, COUNT(DISTINCT t.player_id) AS players, "
        "       SUM(t.total_value) AS gp "
        "FROM data.player_npc_hourly_totals t "
        "JOIN xenforo.dt_npc n ON n.npc_id = t.npc_id "
        "WHERE t.`partition` >= :part_lo "
        "GROUP BY n.npc_name ORDER BY gp DESC LIMIT :top"
    ), {"part_lo": part_lo, "top": top})
    out = [(name, int(players)) for name, players, _gp in rows]
    cache.put("discovery", f"{part_lo}:{top}", out)
    return out


def fetch_notable_loot(conn, npc_names: list[str], cache: StatsCache,
                       ) -> dict[str, list[tuple[str, float, bool]]]:
    """npc_name -> [(item_name, per-kill probability, stackable)], rarest
    first, within the rarity window. Item names come from data.items via the
    join, so they are import-safe by construction."""
    hits, misses = _split_cached(cache, "loot", npc_names)
    out = {npc: [tuple(e) for e in entries] for npc, entries in hits.items()}
    if misses:
        rows = conn.execute(text(
            "SELECT n.npc_name, i.item_name, MAX(l.rarity * l.rolls) AS p, "
            "       MAX(i.stackable) AS stackable "
            "FROM xenforo.dt_npc_loot l "
            "JOIN xenforo.dt_npc n ON n.npc_id = l.npc_id "
            "JOIN data.items i ON i.item_id = l.item_id "
            "WHERE n.npc_name IN :npcs AND l.noted = 0 "
            "GROUP BY n.npc_name, i.item_name "
            "HAVING p >= :lo AND p <= :hi"
        ), {"npcs": misses, "lo": MIN_NOTABLE_P, "hi": MAX_NOTABLE_P})
        fresh: dict[str, list] = {npc: [] for npc in misses}
        for npc, item, p, stackable in rows:
            fresh[npc].append((item, float(p), bool(stackable)))
        for npc, entries in fresh.items():
            entries.sort(key=lambda t: t[1])
            cache.put("loot", npc, entries)
            out[npc] = entries
    return {npc: entries for npc, entries in out.items() if entries}


def fetch_item_values(conn, item_names: set[str],
                      cache: StatsCache) -> dict[str, float]:
    """item_name -> per-unit GE value from each item's most recent recorded
    drop (data.drops.value is unit price). Pure index lookups — averaging over
    a date window scans millions of rows for common items and times out.
    Items never dropped are absent (treated as valueless)."""
    hits, misses = _split_cached(cache, "item_value", sorted(item_names))
    known = {name: float(v) for name, v in hits.items() if v}
    if not misses:
        return known
    item_names = set(misses)
    print(f"Fetching latest unit values for {len(item_names)} items...",
          file=sys.stderr)
    id_rows = conn.execute(
        text("SELECT item_id, item_name FROM data.items WHERE item_name IN :names"),
        {"names": sorted(item_names)}).fetchall()
    id_to_name = {int(iid): name for iid, name in id_rows}
    out: dict[str, float] = {}
    if id_to_name:
        latest = conn.execute(text(
            "SELECT item_id, MAX(drop_id) FROM data.drops "
            "WHERE item_id IN :ids GROUP BY item_id"
        ), {"ids": sorted(id_to_name)}).fetchall()
        drop_ids = [int(did) for _, did in latest if did is not None]
        if drop_ids:
            rows = conn.execute(text(
                "SELECT item_id, value FROM data.drops WHERE drop_id IN :ids"
            ), {"ids": drop_ids})
            for iid, value in rows:
                if value is None:
                    continue
                name = id_to_name.get(int(iid))
                if name is not None:
                    out[name] = max(out.get(name, 0.0), float(value))
    for name in item_names:
        cache.put("item_value", name, out.get(name, 0.0))
    return known | out


def fetch_rates(conn, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """(npc_name, item_name) -> per-kill probability, for curated pieces."""
    out: dict[tuple[str, str], float] = {}
    if not pairs:
        return out
    rows = conn.execute(text(
        "SELECT n.npc_name, i.item_name, MAX(l.rarity * l.rolls) "
        "FROM xenforo.dt_npc_loot l "
        "JOIN xenforo.dt_npc n ON n.npc_id = l.npc_id "
        "JOIN data.items i ON i.item_id = l.item_id "
        "WHERE n.npc_name IN :npcs AND i.item_name IN :items "
        "GROUP BY n.npc_name, i.item_name"
    ), {"npcs": sorted({n for n, _ in pairs}),
        "items": sorted({i for _, i in pairs})})
    for npc, item, p in rows:
        if (npc, item) in pairs:
            out[(npc, item)] = float(p)
    return out


def resolve_items(conn, names: set[str]) -> set[str]:
    """Subset of names that exist verbatim in data.items (curated pieces only;
    discovered pieces are DB-sourced already)."""
    if not names:
        return set()
    rows = conn.execute(
        text("SELECT DISTINCT item_name FROM data.items WHERE item_name IN :names"),
        {"names": sorted(names)},
    )
    return {name for (name,) in rows}


def fetch_pb_cycles(conn, npcs: list[str], inflation: float, cache: StatsCache,
                    min_samples: int = 30) -> dict[str, float]:
    """Raw average-PB seconds are cached; inflation applies on the way out so
    changing --pb-inflation never invalidates the cache. 0 = no usable PBs."""
    hits, misses = _split_cached(cache, "pb_cycle", npcs)
    out = {npc: float(sec) * inflation for npc, sec in hits.items() if sec}
    if misses:
        print(f"Fetching PB averages for {len(misses)} NPCs...", file=sys.stderr)
        rows = conn.execute(text(
            "SELECT n.npc_name, COUNT(*), AVG(pb.personal_best) / 1000 "
            "FROM data.personal_best pb "
            "JOIN xenforo.dt_npc n ON n.npc_id = pb.npc_id "
            "WHERE n.npc_name IN :npcs GROUP BY n.npc_name"
        ), {"npcs": misses})
        fresh = {npc: 0.0 for npc in misses}
        for npc, count, avg in rows:
            if count >= min_samples and avg:
                fresh[npc] = float(avg)
        for npc, sec in fresh.items():
            cache.put("pb_cycle", npc, sec)
            if sec:
                out[npc] = sec * inflation
    return out


def fetch_gap_cycles(conn, npcs: list[str], days: int, cache: StatsCache,
                     min_samples: int = 300) -> dict[str, float]:
    """Avg seconds between a player's consecutive distinct drop timestamps.
    DISTINCT (player_id, date_added) collapses 2x drops from a single death;
    gaps clamped to 20-300s to exclude breaks between sessions. Heaviest
    query in the script — cached per (npc, window), 0 = insufficient data."""
    hits, misses = _split_cached(cache, "gap_cycle",
                                 [f"{npc}:{days}" for npc in npcs])
    out = {key.rsplit(":", 1)[0]: float(sec) for key, sec in hits.items() if sec}
    miss_npcs = [key.rsplit(":", 1)[0] for key in misses]
    if miss_npcs:
        print(f"Estimating kill cycles from drop gaps for {len(miss_npcs)} NPCs "
              f"(can take minutes)...", file=sys.stderr)
        rows = _run_gap_query(conn, miss_npcs, days)
        if rows is not None:  # None = query failed; don't cache dead ends
            fresh = {npc: 0.0 for npc in miss_npcs}
            for npc, count, avg in rows:
                if count >= min_samples and avg:
                    fresh[npc] = float(avg)
            for npc, sec in fresh.items():
                cache.put("gap_cycle", f"{npc}:{days}", sec)
                if sec:
                    out[npc] = sec
    return out


def _run_gap_query(conn, npcs: list[str], days: int):
    try:
        return conn.execute(text(
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
        ), {"npcs": npcs, "days": days}).fetchall()
    except Exception as exc:  # degrade to fallback cycles, not a dead run
        print(f"WARNING: drop-gap estimation failed ({exc.__class__.__name__}); "
              f"relying on PBs/fallback cycles.", file=sys.stderr)
        return None


def fetch_empirical(conn, npcs: list[str], days: int, cache: StatsCache,
                    ) -> dict[str, dict]:
    """npc -> {"completions": int, "items": {item_name: observed events}} from
    our own drops. Distinct (player_id, date_added) events approximate
    completions (all loot from one kill/chest shares a timestamp — the same
    collapse used for 2x drops); item events / completions is then the true
    per-completion drop probability actually observed across the player base.
    This is how conditional-rarity content (raids, DT2) gets absolute rates
    without trusting dt_npc_loot or the wiki. Raw counts are cached so
    EMPIRICAL_MIN_EVENTS changes don't invalidate entries."""
    hits, misses = _split_cached(cache, "empirical",
                                 [f"{npc}:{days}" for npc in npcs])
    out = {key.rsplit(":", 1)[0]: rec for key, rec in hits.items()}
    miss_npcs = [key.rsplit(":", 1)[0] for key in misses]
    if not miss_npcs:
        return out
    print(f"Deriving empirical drop rates for {len(miss_npcs)} NPCs "
          f"({days}d of drops — can take minutes)...", file=sys.stderr)
    try:
        comp_rows = conn.execute(text(
            "SELECT n.npc_name, COUNT(DISTINCT d.player_id, d.date_added) "
            "FROM data.drops d JOIN xenforo.dt_npc n ON n.npc_id = d.npc_id "
            "WHERE n.npc_name IN :npcs "
            "  AND d.date_added > NOW() - INTERVAL :days DAY "
            "GROUP BY n.npc_name"
        ), {"npcs": miss_npcs, "days": days}).fetchall()
        item_rows = conn.execute(text(
            "SELECT n.npc_name, i.item_name, "
            "       COUNT(DISTINCT d.player_id, d.date_added) AS events "
            "FROM data.drops d "
            "JOIN xenforo.dt_npc n ON n.npc_id = d.npc_id "
            "JOIN data.items i ON i.item_id = d.item_id "
            "WHERE n.npc_name IN :npcs "
            "  AND d.date_added > NOW() - INTERVAL :days DAY "
            "GROUP BY n.npc_name, i.item_name HAVING events >= 3"
        ), {"npcs": miss_npcs, "days": days}).fetchall()
    except Exception as exc:
        print(f"WARNING: empirical rate derivation failed "
              f"({exc.__class__.__name__}); those NPCs are skipped this run.",
              file=sys.stderr)
        return out
    fresh = {npc: {"completions": 0, "items": {}} for npc in miss_npcs}
    for npc, completions in comp_rows:
        fresh[npc]["completions"] = int(completions)
    for npc, item, events in item_rows:
        fresh[npc]["items"][item] = int(events)
    for npc, rec in fresh.items():
        cache.put("empirical", f"{npc}:{days}", rec)
        out[npc] = rec
    return out


# ---------------------------------------------------------------------------
# Auto-minting
# ---------------------------------------------------------------------------
def _family_key(item_name: str) -> str | None:
    """Grouping key for set detection: possessive first token ("Dharok's")
    always counts; otherwise the first word unless it's a generic prefix."""
    first = item_name.split()[0]
    if first.endswith("'s"):
        return first
    if first.lower() in FAMILY_STOPWORDS or len(item_name.split()) < 2:
        return None
    return first


def _is_notable(item: str, p: float,
                values: dict[str, float], min_value: float) -> bool:
    """Task-worthiness: valuable, or rare and untradeable (zero value)."""
    if item in ITEM_BLACKLIST or any(
            s in item.lower() for s in ITEM_BLACKLIST_SUBSTRINGS):
        return False
    value = values.get(item, 0.0)
    if value >= min_value:
        return True
    return p <= RARE_NOTABLE_P and value <= 0


def mint_candidates(loot: dict[str, list[tuple[str, float, bool]]],
                    values: dict[str, float], min_value: float) -> list[dict]:
    """Turn per-NPC notable loot into candidate tasks (exact / any_of /
    assembly). Deterministic templates; curated overlay handles the rest."""
    notable: dict[str, list[tuple[str, float]]] = {}
    for npc, items in loot.items():
        keep = [(it, p) for it, p, _stackable in items
                if _is_notable(it, p, values, min_value)]
        if keep:
            notable[npc] = keep

    candidates: list[dict] = []
    # Exact tasks dedupe across NPCs: keep the most obtainable source.
    best_exact: dict[str, tuple[float, str]] = {}
    for npc, items in notable.items():
        for item, p in items:
            cur = best_exact.get(item)
            if cur is None or p > cur[0]:
                best_exact[item] = (p, npc)
    for item, (p, npc) in sorted(best_exact.items()):
        candidates.append({
            "name": item,
            "description": f"Obtain 1x {item} from {npc}.",
            "kind": "exact", "auto": True,
            "pieces": [{"item": item, "npc": npc, "rate": p}],
        })

    for npc, items in sorted(notable.items()):
        # "Any unique" pools take only the genuinely rare drops — including
        # merely-valuable commons (seeds etc.) collapses these to trivial.
        rares = [(it, p) for it, p in items if p <= RARE_NOTABLE_P][:MAX_ANY_OF_ITEMS]
        if len(rares) >= 2:
            shown = ", ".join(it for it, _ in rares[:8])
            more = f" (+{len(rares) - 8} more)" if len(rares) > 8 else ""
            candidates.append({
                "name": f"Any {npc} Unique",
                "description": f"Obtain any notable drop from {npc}: {shown}{more}.",
                "kind": "any_of", "auto": True,
                "pieces": [{"item": it, "npc": npc, "rate": p} for it, p in rares],
            })
        families: dict[str, list[tuple[str, float]]] = {}
        for item, p in items:
            key = _family_key(item)
            if key:
                families.setdefault(key, []).append((item, p))
        for key, pieces in sorted(families.items()):
            if not 2 <= len(pieces) <= MAX_FAMILY_PIECES:
                continue
            names = [it for it, _ in pieces]
            candidates.append({
                "name": f"The {key.rstrip('s').rstrip(chr(39))} Collection"
                        if key.endswith("'s") else f"The {key} Collection",
                "description": f"Assemble from {npc}: " + ", ".join(names) + ".",
                "kind": "assembly", "auto": True,
                "pieces": [{"item": it, "npc": npc, "rate": p} for it, p in pieces],
            })
    return candidates


def _signature(cand: dict) -> tuple:
    return (cand["kind"], frozenset(p["item"] for p in _catalog_items(cand)))


def _catalog_items(cand: dict) -> list[dict]:
    if cand["kind"] == "groups":
        return [p for g in cand["groups"] for p in g["pieces"]]
    if cand["kind"] == "any_path":
        return [p for path in cand["paths"] for g in path["groups"] for p in g["pieces"]]
    return cand["pieces"]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
class SkipCandidate(Exception):
    pass


def _piece_rate(piece: dict, rates: dict) -> float:
    if "rate" in piece:
        return piece["rate"]
    p = rates.get((piece["npc"], piece["item"]))
    if p is None:
        raise SkipCandidate(
            f"no drop rate known for {piece['item']} @ {piece['npc']}")
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
    by_npc: dict[str, list[dict]] = {}
    for p in cand_pieces:
        by_npc.setdefault(p["npc"], []).append(p)
    if kind == "any_of":
        best = None
        for npc, pieces in by_npc.items():
            cycle = _cycle_for(npc, cycles)
            rate_sum = sum(_piece_rate(p, rates) for p in pieces)
            hours = (1.0 / rate_sum) * cycle / 3600
            if best is None or hours < best[0]:
                best = (hours,
                        f"best source {npc}: {rate_sum:.5f} per kill x {cycle:.0f}s")
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
        return _eval_pieces(_groups_to_pieces(cand["groups"], rates),
                            "assembly", concurrent, rates, cycles)
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
    """Absolute-hour tier, or None if over the exclusion cap. Tier is
    independent of max_hours — the cap only decides in/out, not which tier."""
    if hours > max_hours:
        return None
    if hours < TIER_EDGES_HOURS["air"]:
        return "air"
    if hours < TIER_EDGES_HOURS["water"]:
        return "water"
    if hours < TIER_EDGES_HOURS["earth"]:
        return "earth"
    return "fire"


def scale_points(tier: str, hours: float, max_hours: float) -> int:
    edges = TIER_EDGES_HOURS
    if tier == "air":
        lo, hi = 0.0, edges["air"]
    elif tier == "water":
        lo, hi = edges["air"], edges["water"]
    elif tier == "earth":
        lo, hi = edges["water"], edges["earth"]
    else:  # fire is open-ended; scale up to the cap (min 1h span)
        lo, hi = edges["earth"], max(max_hours, edges["earth"] + 1)
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
        "_npc": _catalog_items(cand)[0]["npc"],
        "_auto": bool(cand.get("auto")),
        "_reasoning": reasoning,
    }


def select_balanced(tasks: list[dict], count: int) -> tuple[list[dict], list[dict]]:
    """Pick `count` tasks round-robin across tiers, rarest tier first
    (fire, earth, water, air). Within a tier, prefer NPCs not already
    represented so a small selection spans the game. Deterministic."""
    by_tier: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for task in tasks:
        by_tier[task["difficulty"]].append(task)
    order = ["fire", "earth", "water", "air"]
    chosen: set[int] = set()
    npc_used: dict[str, int] = {}
    total = min(count, len(tasks))
    while len(chosen) < total:
        progressed = False
        for tier in order:
            if len(chosen) >= total:
                break
            pool = [t for t in by_tier[tier] if id(t) not in chosen]
            if not pool:
                continue
            pick = min(pool, key=lambda t: npc_used.get(t["_npc"], 0))
            chosen.add(id(pick))
            npc_used[pick["_npc"]] = npc_used.get(pick["_npc"], 0) + 1
            progressed = True
        if not progressed:
            break
    selected = [t for t in tasks if id(t) in chosen]
    left_out = [t for t in tasks if id(t) not in chosen]
    return selected, left_out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def generate(conn, target_max_hours: float = 15.0, pb_inflation: float = 1.4,
             gap_days: int = 90, activity_days: int = 90, top_npcs: int = 25,
             only_npcs: list[str] | None = None, min_value: float = 50000,
             empirical_days: int = 120, exclude_npcs: list[str] | None = None,
             cache: StatsCache | None = None,
             target_count: int | None = None) -> tuple[list[dict], dict]:
    """Returns (tasks, meta). meta carries excluded/skipped/discovery info."""
    if cache is None:
        cache = StatsCache(None, 0)  # disabled: no reads, no writes
    exclude = set(exclude_npcs or [])

    def _excluded(name: str) -> bool:
        return name in exclude or name.startswith(NON_KILL_SOURCE_PREFIXES)

    if only_npcs:
        # Explicit --npc is honored as-is except for --exclude-npc overrides;
        # the non-kill prefixes still apply so a stray clue name is skipped.
        active = [(n, 0) for n in only_npcs if not _excluded(n)]
    else:
        # Over-fetch so excluded sources (clues etc.) don't consume top slots.
        discovered = discover_active_npcs(conn, activity_days, top_npcs + 15, cache)
        active = [(n, p) for n, p in discovered if not _excluded(n)][:top_npcs]
    skipped_npcs: list[dict] = []
    table_npcs = [n for n, _ in active if n not in EMPIRICAL_RATE_NPCS]
    empirical_npcs = [n for n, _ in active if n in EMPIRICAL_RATE_NPCS]

    loot = fetch_notable_loot(conn, table_npcs, cache)
    empirical = fetch_empirical(conn, empirical_npcs, empirical_days, cache)
    empirical_summary: dict[str, int] = {}
    for npc, rec in empirical.items():
        completions = rec.get("completions") or 0
        empirical_summary[npc] = completions
        if completions < 100:
            skipped_npcs.append({"npc": npc, "reason":
                                 f"only {completions} completions observed "
                                 f"in {empirical_days}d — too few for rates"})
            continue
        entries = [
            (item, events / completions, False)
            for item, events in rec.get("items", {}).items()
            if events >= EMPIRICAL_MIN_EVENTS
            and MIN_NOTABLE_P <= events / completions <= MAX_NOTABLE_P
        ]
        if entries:
            loot[npc] = sorted(entries, key=lambda t: t[1])
    values = fetch_item_values(
        conn, {it for items in loot.values() for it, _, _ in items}, cache)
    candidates = mint_candidates(loot, values, min_value)

    # Curated overlay: curated entries win on identical (kind, item-set).
    curated_ok: list[dict] = []
    curated_names = {p["item"] for c in CURATED for p in _catalog_items(c)}
    found = resolve_items(conn, curated_names)
    for cand in CURATED:
        missing = [p["item"] for p in _catalog_items(cand) if p["item"] not in found]
        if missing:
            skipped_npcs.append({"npc": cand["name"],
                                 "reason": f"unresolved items: {missing}"})
            continue
        curated_ok.append(cand)
    curated_sigs = {_signature(c) for c in curated_ok}
    candidates = curated_ok + [c for c in candidates
                               if _signature(c) not in curated_sigs]

    # Rates for curated pieces without overrides: empirical where available,
    # dt_npc_loot otherwise.
    empirical_rates = {(npc, item): entry[1]
                       for npc in empirical_summary
                       for entry in loot.get(npc, [])
                       for item in [entry[0]]}
    rate_pairs = {(p["npc"], p["item"]) for c in candidates
                  for p in _catalog_items(c)
                  if "rate" not in p and (p["npc"], p["item"]) not in empirical_rates}
    rates = {**fetch_rates(conn, rate_pairs), **empirical_rates}

    # Kill cycles: overrides, then PBs, then drop gaps for the remainder.
    need_cycle = sorted({p["npc"] for c in candidates for p in _catalog_items(c)}
                        | {c["attempt_npc"] for c in candidates
                           if "expected_attempts" in c})
    cycles: dict[str, float] = {}
    cycle_sources: dict[str, str] = {}
    for npc in need_cycle:
        if "cycle_seconds" in CONTEXTS.get(npc, {}):
            cycles[npc] = CONTEXTS[npc]["cycle_seconds"]
            cycle_sources[npc] = "override"
    pb = fetch_pb_cycles(conn, [n for n in need_cycle if n not in cycles],
                         pb_inflation, cache)
    for npc, sec in pb.items():
        cycles[npc], cycle_sources[npc] = sec, "pb"
    gap = fetch_gap_cycles(conn, [n for n in need_cycle if n not in cycles],
                           gap_days, cache)
    for npc, sec in gap.items():
        cycles[npc], cycle_sources[npc] = sec, "gap"
    for npc in need_cycle:
        if npc not in cycles and "fallback_cycle" in CONTEXTS.get(npc, {}):
            cycles[npc] = CONTEXTS[npc]["fallback_cycle"]
            cycle_sources[npc] = "fallback"

    tasks, excluded, skipped, too_quick = [], [], [], 0
    seen_names: set[str] = set()
    for cand in candidates:
        try:
            hours, note = eval_candidate(cand, rates, cycles)
        except SkipCandidate as exc:
            skipped.append({"name": cand["name"], "reason": str(exc)})
            continue
        if hours < MIN_TASK_HOURS:
            too_quick += 1
            continue
        tier = bucket(hours, target_max_hours)
        if tier is None:
            excluded.append({"name": cand["name"], "est_hours": round(hours, 1),
                             "math": note})
            continue
        name = cand["name"]
        if name.lower() in seen_names:  # library upserts case-insensitively
            name = cand["name"] + f" ({_catalog_items(cand)[0]['npc']})"
            cand = {**cand, "name": name}
        seen_names.add(name.lower())
        tasks.append(build_task(cand, tier, hours, note, target_max_hours))

    left_out: list[dict] = []
    if target_count is not None and target_count < len(tasks):
        tasks, left_out = select_balanced(tasks, target_count)

    meta = {
        "source": SOURCE,
        "generated": date.today().isoformat(),
        "generator": "scripts/generate_item_set_tasks.py",
        "target_max_hours": target_max_hours,
        "difficulty_bands_hours": {
            "air": f"<{TIER_EDGES_HOURS['air']}", "water": f"<{TIER_EDGES_HOURS['water']}",
            "earth": f"<{TIER_EDGES_HOURS['earth']}",
            "fire": f"{TIER_EDGES_HOURS['earth']}-{target_max_hours}"},
        "discovered_npcs": [{"npc": n, "players": p} for n, p in active],
        "empirical_completions": empirical_summary,
        "empirical_days": empirical_days,
        "kill_cycles_seconds": {n: round(cycles[n]) for n in sorted(cycles)},
        "kill_cycle_sources": cycle_sources,
        "excluded_over_cap": sorted(excluded, key=lambda e: -e["est_hours"]),
        "skipped": skipped + skipped_npcs,
        "dropped_as_filler_under_min_hours": too_quick,
        "eligible_not_selected": [
            {"name": t["name"], "difficulty": t["difficulty"]} for t in left_out],
    }
    return tasks, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-hours", type=float, default=15.0,
                    help="expected-hours cap; tasks above this are excluded (default 15)")
    ap.add_argument("--count", type=int, default=None,
                    help="target number of tasks: tier-balanced, NPC-diverse "
                         "selection from everything that fits under --max-hours")
    ap.add_argument("--top-npcs", type=int, default=25,
                    help="how many most-active NPCs to mint tasks from (default 25)")
    ap.add_argument("--activity-days", type=int, default=90,
                    help="activity window for NPC discovery (default 90)")
    ap.add_argument("--npc", action="append", default=None, metavar="NAME",
                    help="skip discovery and mint only for these NPCs (repeatable)")
    ap.add_argument("--exclude-npc", action="append", default=None, metavar="NAME",
                    help="exclude these NPCs from discovery (repeatable); clue "
                         "scrolls and reward caskets are always excluded")
    ap.add_argument("--min-value", type=float, default=50000,
                    help="unit GE value for an item to be notable regardless of "
                         "rarity (default 50k); rarer than 1/50 qualifies anyway")
    ap.add_argument("--empirical-days", type=int, default=120,
                    help="drops window for empirical per-completion rates on "
                         "conditional-rarity NPCs like raids (default 120)")
    ap.add_argument("--cache-file", default=CACHE_DEFAULT,
                    help="flat-file stats cache path (default "
                         "scripts/.event_task_stats_cache.json)")
    ap.add_argument("--cache-ttl-days", type=float, default=7,
                    help="age at which cached stats expire (default 7)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached stats this run (recompute + rewrite)")
    ap.add_argument("--no-cache", action="store_true",
                    help="disable the stats cache entirely (no reads or writes)")
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
    # which the discovery/gap scans (minutes over months of drops) exceed.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    from db.models.base import DB_USER, DB_PASS
    engine = create_engine(
        f"mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data",
        poolclass=NullPool,
        connect_args={"connect_timeout": 10, "read_timeout": 600, "charset": "utf8mb4"},
    )
    cache = StatsCache(None if args.no_cache else args.cache_file,
                       args.cache_ttl_days, read_enabled=not args.refresh)
    try:
        with engine.connect() as conn:
            tasks, meta = generate(
                conn, target_max_hours=args.max_hours,
                pb_inflation=args.pb_inflation, gap_days=args.gap_days,
                activity_days=args.activity_days, top_npcs=args.top_npcs,
                only_npcs=args.npc, min_value=args.min_value,
                empirical_days=args.empirical_days, exclude_npcs=args.exclude_npc,
                cache=cache, target_count=args.count)
    finally:
        cache.save()

    by_tier: dict[str, int] = {}
    for t in tasks:
        by_tier[t["difficulty"]] = by_tier.get(t["difficulty"], 0) + 1
    print(f"Generated {len(tasks)} tasks (cap {args.max_hours}h): "
          + ", ".join(f"{t}={by_tier.get(t, 0)}" for t in TIER_ORDER))
    for t in tasks:
        origin = "auto" if t["_auto"] else "curated"
        print(f"  [{t['difficulty']:>5}] {t['default_points']:>3}pts {origin:>7}  "
              f"{t['name']}: {t['_reasoning']}")
    if meta["excluded_over_cap"]:
        print(f"\nExcluded over {args.max_hours}h cap: "
              + "; ".join(f"{e['name']} ({e['est_hours']}h)"
                          for e in meta["excluded_over_cap"]))
    if meta["skipped"]:
        print("\nSkipped:")
        for s in meta["skipped"]:
            print(f"  {s.get('name') or s.get('npc')}: {s['reason']}")
    if meta["dropped_as_filler_under_min_hours"]:
        print(f"\nDropped {meta['dropped_as_filler_under_min_hours']} candidates "
              f"under {MIN_TASK_HOURS}h as filler.")
    if meta["eligible_not_selected"]:
        print(f"\nEligible but not selected (--count {args.count}): "
              + ", ".join(f"{t['name']} [{t['difficulty']}]"
                          for t in meta["eligible_not_selected"]))
    if args.count is not None and len(tasks) < args.count:
        print(f"\nNOTE: only {len(tasks)} tasks fit under the {args.max_hours}h cap; "
              f"raise --top-npcs or --max-hours for more.")

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
