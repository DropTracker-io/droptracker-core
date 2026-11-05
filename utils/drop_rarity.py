"""Per-kill drop rate of an item, for the ``{item_rarity}`` notification token.

The rates come from ``xenforo.dt_npc_loot``, the wiki drop table that
``scripts/sync_wiki_drops.py`` resyncs weekly. The game cache does not carry
drop tables (they are server-side), so the wiki is the only source.

The table has no index on (npc, item), so a lookup per notification would be a
~100ms scan. Instead the whole table (~106k rows, ~10MB as tuples) is held in
process and reloaded every few hours in a background thread. Only the first
lookup in a process waits for the load (~1s).

The data is messy, and a wrong rate is worse than none, so :func:`drop_rate`
returns ``None`` unless the answer is clear:

* A drop-table line repeats per wiki revision and per multi-form NPC id, and
  some NPCs list the same item at very different rates (Fortis Colosseum per
  wave, troll variants). When the candidate lines disagree by more than
  :data:`AMBIGUOUS_RATIO`, there is no single answer and nothing is shown.
* Raids store the odds *within* the purple table (Twisted bow "1/30"), not per
  completion, and clue caskets store a per-roll rate for a casket that rolls
  several times. Those sources are skipped entirely.
* "Always" lines (rarity 1) say nothing worth showing.
* A line on a multi-roll table (Barrows 7, Zulrah 2) becomes a per-kill chance,
  ``1 - (1 - p) ** rolls``. Where the same item is listed both on a single-roll
  line and a multi-roll one, the multi-roll line is a conditional sub-table
  (a "0.5 on 3 rolls" uncut diamond) and is ignored.

Fails open: a DB error leaves the token empty, so the line holding it drops.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

log = logging.getLogger(__name__)

#: Reload interval. The source table changes once a week.
REFRESH_SECONDS = 6 * 3600
#: After a failed load, wait this long before trying again.
RETRY_SECONDS = 300
#: Candidate rates further apart than this are a conflict, not a rounding gap.
AMBIGUOUS_RATIO = 2.0

#: Sources whose stored rarity is not a per-completion chance: raids (odds
#: within the purple table) and clue caskets (per roll, several rolls each).
CONDITIONAL_SOURCE_PREFIXES = ("chambers of xeric", "theatre of blood", "tombs of amascut",
                               "clue scroll", "reward casket")

# One drop-table line: (quantity ranges, rarity per roll, rolls).
Line = Tuple[Tuple[Tuple[int, int], ...], float, int]

_LOAD_SQL = (
    "SELECT l.npc_id, n.npc_name, i.item_name, l.quantity, l.rarity, l.rolls "
    "FROM xenforo.dt_npc_loot l "
    "JOIN npc_list n ON n.npc_id = l.npc_id "
    "JOIN items i ON i.item_id = l.item_id "
    "WHERE l.rarity > 0 AND l.rarity < 1"
)

_lock = threading.Lock()
_table: Optional["RarityTable"] = None
_loaded_at = 0.0
_failed_at = 0.0
_refreshing = False

_QTY_PART = re.compile(r"^\s*(\d[\d,]*)\s*(?:[-–]\s*(\d[\d,]*))?\s*$")


def parse_quantity(raw) -> Tuple[Tuple[int, int], ...]:
    """Wiki quantity text -> inclusive ranges. ``"5-10 (noted)"`` -> ((5, 10),).

    ``"1, 3"`` lists alternatives; ``"1,000"`` (no space) is one number.
    Anything unparseable gives ``()``, which matches no quantity (the line
    still counts when quantity is unknown).
    """
    text = re.sub(r"\(.*?\)", "", str(raw or ""))
    out = []
    for part in re.split(r"[;,]\s+|;", text):
        m = _QTY_PART.match(part)
        if not m:
            continue
        lo = int(m.group(1).replace(",", ""))
        hi = int((m.group(2) or m.group(1)).replace(",", ""))
        out.append((min(lo, hi), max(lo, hi)))
    return tuple(out)


def _npc_key(name) -> str:
    try:
        from utils.npc_names import npc_match_key
        return npc_match_key(name) or ""
    except Exception:
        return str(name or "").strip().lower()


def _item_key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _per_kill(rarity: float, rolls: int) -> float:
    if rolls <= 1:
        return rarity
    return 1.0 - (1.0 - rarity) ** rolls


def _resolve_lines(lines: Iterable[Line], quantity: Optional[int]) -> Optional[float]:
    """One NPC's lines for one item -> its per-kill chance, or None if unclear."""
    lines = set(lines)
    if not lines:
        return None
    if quantity is not None:
        matched = {ln for ln in lines if any(lo <= quantity <= hi for lo, hi in ln[0])}
        if matched:
            lines = matched
    if any(ln[2] <= 1 for ln in lines):
        lines = {ln for ln in lines if ln[2] <= 1}
    rates = [_per_kill(ln[1], ln[2]) for ln in lines]
    return _pick(rates)


def _pick(rates) -> Optional[float]:
    rates = [r for r in rates if r and 0 < r < 1]
    if not rates:
        return None
    lo, hi = min(rates), max(rates)
    if hi / lo > AMBIGUOUS_RATIO:
        return None
    # Near-duplicates (revisions, rounding): the commoner reading, so the
    # message never overstates how rare the drop was.
    return hi


class RarityTable:
    """The wiki drop table, indexed for :meth:`rate`."""

    def __init__(self, rows: Iterable):
        by_id: Dict[Tuple[int, str], set] = {}
        ids_by_name: Dict[str, set] = {}
        for npc_id, npc_name, item_name, quantity, rarity, rolls in rows:
            try:
                rarity = float(rarity)
                npc_id = int(npc_id)
            except (TypeError, ValueError):
                continue
            if not (0 < rarity < 1) or not item_name or not npc_name:
                continue
            if str(npc_name).strip().lower().startswith(CONDITIONAL_SOURCE_PREFIXES):
                continue
            line = (parse_quantity(quantity), rarity, int(rolls or 1))
            by_id.setdefault((npc_id, _item_key(item_name)), set()).add(line)
            ids_by_name.setdefault(_npc_key(npc_name), set()).add(npc_id)
        self.by_id = {k: frozenset(v) for k, v in by_id.items()}
        self.ids_by_name = {k: tuple(sorted(v)) for k, v in ids_by_name.items()}

    def __len__(self):
        return len(self.by_id)

    def rate(self, item_name, npc_name=None, npc_id=None, quantity=None) -> Optional[float]:
        """Per-kill chance of ``item_name`` from this NPC, or None when unknown/unclear.

        ``npc_id`` (the drop's own id) is tried first; otherwise, or when that id
        has no line for the item, every id sharing the NPC's name is consulted
        and they must agree.
        """
        item = _item_key(item_name)
        if not item:
            return None
        if npc_name and str(npc_name).strip().lower().startswith(CONDITIONAL_SOURCE_PREFIXES):
            return None
        nid = _as_int(npc_id)
        if nid is not None and (nid, item) in self.by_id:
            return _resolve_lines(self.by_id[(nid, item)], quantity)
        if not npc_name:
            return None
        rates = []
        for other in self.ids_by_name.get(_npc_key(npc_name), ()):
            lines = self.by_id.get((other, item))
            if lines:
                r = _resolve_lines(lines, quantity)
                if r is None:
                    return None
                rates.append(r)
        return _pick(rates)


def _as_int(value) -> Optional[int]:
    try:
        text = str(value).strip().strip("`").replace(",", "")
        return int(text) if text not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def format_rate(p: Optional[float]) -> Optional[str]:
    """``0.00390625`` -> ``"1 in 256 (0.391%)"``. None stays None."""
    if p is None or not (0 < p < 1):
        return None
    n = 1.0 / p
    if n >= 100:
        one_in = f"{round(n):,}"
    elif n >= 10:
        one_in = f"{n:.1f}".rstrip("0").rstrip(".")
    else:
        one_in = f"{n:.2f}".rstrip("0").rstrip(".")
    pct = p * 100
    pct_text = "<0.001" if pct < 0.001 else f"{pct:.3g}"
    if "e" in pct_text:
        pct_text = f"{pct:.6f}".rstrip("0").rstrip(".")
    return f"1 in {one_in} ({pct_text}%)"


def _load_rows():
    from sqlalchemy import text
    from db import Session

    s = Session()
    try:
        return list(s.execute(text(_LOAD_SQL)).fetchall())
    finally:
        try:
            s.rollback()
        finally:
            s.close()


def _load() -> None:
    global _table, _loaded_at, _failed_at, _refreshing
    try:
        table = RarityTable(_load_rows())
        with _lock:
            _table, _loaded_at = table, time.monotonic()
    except Exception as e:  # fail open
        log.warning("drop rarity table load failed: %s", e)
        with _lock:
            _failed_at = time.monotonic()
    finally:
        with _lock:
            _refreshing = False


def get_table() -> Optional[RarityTable]:
    """The loaded table. Blocks only on a process's first load; later reloads
    run in a background thread and the old table serves meanwhile."""
    global _refreshing
    now = time.monotonic()
    with _lock:
        table, stale = _table, now - _loaded_at > REFRESH_SECONDS
        recently_failed = _failed_at and now - _failed_at < RETRY_SECONDS
        if table is not None and not stale:
            return table
        if recently_failed or _refreshing:
            return table
        _refreshing = True
    if table is None:
        _load()
        return _table
    threading.Thread(target=_load, name="drop-rarity-refresh", daemon=True).start()
    return table


def set_table(table: Optional[RarityTable]) -> None:
    """Install a table directly (tests, warm-up)."""
    global _table, _loaded_at, _failed_at, _refreshing
    with _lock:
        _table, _failed_at, _refreshing = table, 0.0, False
        _loaded_at = time.monotonic() if table is not None else 0.0


def rarity_text(item_name, npc_name=None, npc_id=None, quantity=None) -> Optional[str]:
    """``"1 in 256 (0.391%)"`` for the drop, or None when there is no clear rate."""
    if not item_name or not (npc_name or npc_id not in (None, "")):
        return None
    try:
        table = get_table()
        if table is None:
            return None
        return format_rate(table.rate(item_name, npc_name, npc_id, _as_int(quantity)))
    except Exception as e:
        log.warning("drop rarity lookup failed: %s", e)
        return None
