"""Pure logic for account state snapshots: validation, decoding and diffing.

Deliberately stdlib-only and free of database access so it can be tested
directly (and under the unit-test conftest, which stubs ``db``). The route owns
persistence; everything that decides *what changed* lives here.

The diff exists to answer one question: which of these changes is worth telling
somebody about? That is harder than it looks, because the first snapshot from a
long-standing account looks identical to a player completing nine hundred things
at once.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── Limits ───────────────────────────────────────────────────────────────────
# A snapshot is attacker-controllable, so every collection is capped before it
# reaches the database. The caps are far above any legitimate account.
MAX_ITEMS = 5000
MAX_QUESTS = 500
MAX_VARPS = 128
MAX_DIARY_TIERS = 100
MAX_SKILLS = 40

# OSRS experience is capped at 200m per skill; anything above is a bad client or
# a hostile one.
MAX_SKILL_XP = 200_000_000

# ── Event-suppression guards ─────────────────────────────────────────────────
# A player whose stored collection log is (nearly) empty and who suddenly
# reports many items has just done their first full read. Those items are years
# old; announcing them would be wrong and very loud.
LATE_INIT_KNOWN_ITEMS_MAX = 10
LATE_INIT_NEW_ITEMS_MIN = 10


def _coerce_int(value: Any) -> Optional[int]:
    """Int, or None when the value is not one. Bools are rejected deliberately —
    ``True`` is an int in Python and would silently become item id 1."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def parse_int_map(raw: Any, *, limit: int, min_key: int = 0,
                  min_value: Optional[int] = 0, max_value: Optional[int] = None) -> Dict[int, int]:
    """Normalises a JSON object into {int: int}, dropping anything malformed.

    JSON object keys are always strings, so the client's {itemId: quantity}
    arrives as {"995": 1}. Entries that cannot be coerced are skipped rather
    than failing the whole snapshot: one bad pair should not cost a player their
    entire sync.

    ``min_value=None`` disables the lower bound, which combat achievement varps
    need: the client sends a signed 32-bit int, so a varp whose top bit is set
    arrives negative. Rejecting those would silently discard 32 completed tasks
    per affected varp — data loss with no error anywhere.
    """
    if not isinstance(raw, dict):
        return {}

    out: Dict[int, int] = {}
    for key, value in raw.items():
        if len(out) >= limit:
            break
        k = _coerce_int(key)
        v = _coerce_int(value)
        if k is None or v is None:
            continue
        if k < min_key:
            continue
        if min_value is not None and v < min_value:
            continue
        if max_value is not None and v > max_value:
            continue
        out[k] = v
    return out


def parse_diary_tiers(raw: Any, *, limit: int = MAX_DIARY_TIERS) -> List[Tuple[int, int, int]]:
    """Normalises the diary tier list into (area_id, tier, completed) triples."""
    if not isinstance(raw, list):
        return []

    out: List[Tuple[int, int, int]] = []
    for entry in raw:
        if len(out) >= limit:
            break
        if not isinstance(entry, dict):
            continue
        area = _coerce_int(entry.get("area_id"))
        tier = _coerce_int(entry.get("tier"))
        completed = _coerce_int(entry.get("completed"))
        if area is None or tier is None or completed is None:
            continue
        if area < 0 or tier < 0 or completed < 0:
            continue
        out.append((area, tier, completed))
    return out


def parse_skills(raw: Any) -> Dict[str, int]:
    """Skill name -> xp, bounded by the game's own maximum."""
    if not isinstance(raw, dict):
        return {}

    out: Dict[str, int] = {}
    for name, xp in raw.items():
        if len(out) >= MAX_SKILLS:
            break
        if not isinstance(name, str) or not name.strip():
            continue
        value = _coerce_int(xp)
        if value is None or value < 0 or value > MAX_SKILL_XP:
            continue
        out[name.strip()[:32]] = value
    return out


def count_completed_combat_achievements(varps: Dict[int, int]) -> int:
    """Number of completed combat achievement tasks.

    A population count over the completion bits. Order-independent on purpose:
    mapping a bit back to a *specific* task additionally requires knowing the
    varp ordering and a task registry, and getting that wrong would mislabel
    achievements. Counting is correct without either.
    """
    total = 0
    for value in varps.values():
        if not value:
            continue
        # Mask to 32 bits *before* counting. The client sends a signed int, so a
        # varp with its top bit set arrives negative — skipping negatives (or
        # counting Python's infinite two's-complement representation) both give
        # the wrong answer.
        total += bin(value & 0xFFFFFFFF).count("1")
    return total


def new_collection_log_items(previous: Dict[int, int],
                             incoming: Dict[int, int]) -> List[int]:
    """Item ids present in the snapshot that we had no record of before."""
    return [item_id for item_id, qty in incoming.items()
            if qty > 0 and item_id not in previous]


def is_late_collection_log_init(known_count: int, new_count: int) -> bool:
    """True when a batch of "new" items is really a first full read.

    Without this, the first person to open their collection log after enabling
    sync would generate hundreds of "new item!" events for items they obtained
    years ago.
    """
    return known_count < LATE_INIT_KNOWN_ITEMS_MAX and new_count > LATE_INIT_NEW_ITEMS_MIN


def newly_completed_quests(previous: Dict[int, int],
                           incoming: Dict[int, int]) -> List[int]:
    """Quests that went from not-finished to finished.

    A quest absent from ``previous`` is not treated as newly completed: we have
    simply never seen this player's quest list before.
    """
    QUEST_FINISHED = 2
    return [
        quest_id for quest_id, state in incoming.items()
        if state == QUEST_FINISHED
        and quest_id in previous
        and previous[quest_id] != QUEST_FINISHED
    ]


def improved_diary_tiers(previous: Dict[Tuple[int, int], int],
                         incoming: Iterable[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    """Diary tiers whose completed count increased since the last snapshot."""
    out = []
    for area_id, tier, completed in incoming:
        before = previous.get((area_id, tier))
        if before is not None and completed > before:
            out.append((area_id, tier, completed))
    return out


def _size(value: Any) -> int:
    """Length of a collection, 0 for anything else.

    Total by design. This feeds a log line, and the input is attacker-supplied:
    a plain ``len()`` on ``{"quests": 12345}`` raises, and a summary that can
    raise turns a successful sync into a 500 *after* it has already committed.
    """
    if isinstance(value, (dict, list, tuple, set, str)):
        return len(value)
    return 0


def snapshot_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, log-safe description of a snapshot.

    Sizes rather than contents: a snapshot is thousands of ids and logging it
    whole would be unreadable and needlessly revealing.
    """
    if not isinstance(snapshot, dict):
        return {}
    return {
        "source": snapshot.get("source"),
        "manifest": snapshot.get("manifest_version"),
        "skills": _size(snapshot.get("skills")),
        "quests": _size(snapshot.get("quests")),
        "ca_varps": _size(snapshot.get("ca_varps")),
        "diary_tiers": _size(snapshot.get("diary_tiers")),
        "items": _size(snapshot.get("items")),
        "clog_complete": bool(snapshot.get("clog_complete")),
    }


def serialize_varps(varps: Dict[int, int]) -> str:
    """Stable JSON for the stored varp blob, so equal state compares equal."""
    return json.dumps({str(k): v for k, v in sorted(varps.items())},
                      separators=(",", ":"))


def deserialize_varps(raw: Optional[str]) -> Dict[int, int]:
    """Stored varp blob back into {varp_id: value}; {} if unreadable."""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    # min_value=None: varps are signed and legitimately negative.
    return parse_int_map(loaded, limit=MAX_VARPS, min_value=None)
