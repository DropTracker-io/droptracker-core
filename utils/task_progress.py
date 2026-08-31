# utils/task_progress.py — the pure "how far along is this?" folds.
#
# Extracted verbatim from services/event_engine.py (which still owns the
# session-backed wrappers around them) so that a SECOND scorer can reuse the
# task vocabulary without importing the engine: services/competition.py folds
# competition bonus rules whose criteria are an embedded task config, and the
# pytest conftest MagicMocks the whole ``services`` package — a service may not
# import a sibling service and see real code. ``utils`` loads for real
# (utils.osrs_pets / utils.ca_tasks are the precedent), so the shared cores
# live here.
#
# Everything in this module is a pure function of ledger rows + config. No
# db/redis/service imports, stdlib only — that is the property that lets both
# callers trust it, and the reason event_engine keeps only thin delegating
# wrappers behind its original private names.
#
# A "row" is anything with ``matched_target`` / ``quantity`` / ``player_id`` /
# ``source_type`` / ``note`` attributes (an EventCompletion, or a stub in
# tests). Rows whose ``source_type`` is ``"bonus"`` are board-game coin awards
# and never count as progress — note that competition bonus rows carry the
# ENVELOPE kind as their source_type ("drop", "clog", …), so they are correctly
# included by the folds below.

from __future__ import annotations

from typing import Optional

# Metric paths an ``any_path`` task may branch on (mirrored by the write
# validator's own copy in web_api/routes/event_task_validation.py).
PATH_METRICS = ("kc", "loot_value")

_PATH_NOTE_PREFIX = "path:"

# Progress shapes the dispatcher understands. The competition validator derives
# one of these per bonus rule so services/competition.py needs no task-type
# vocabulary of its own.
PROGRESS_KINDS = ("count", "distinct", "groups", "any_path", "points")


def norm(value) -> str:
    """Normalize a name for case-insensitive comparison — must stay identical
    to ``services.event_engine._norm`` (ledger ``matched_target`` values are
    written by the engine and read here)."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _qty(row) -> int:
    return max(int(getattr(row, "quantity", 1) or 1), 1)


def _is_coin_bonus(row) -> bool:
    """Board-game coin award rows — never progress."""
    return (getattr(row, "source_type", None) or "") == "bonus"


def path_need(path: dict) -> int:
    try:
        return max(int(path.get("need") or 0), 1)
    except (TypeError, ValueError):
        return 1


def row_path_idx(row) -> Optional[int]:
    """Metric-path index a ledger row was recorded for, parsed from the
    engine's ``note`` tag (``path:N`` or ``path:N | admin note``); None for
    item/manual/bonus rows."""
    note = getattr(row, "note", None)
    if not isinstance(note, str) or not note.startswith(_PATH_NOTE_PREFIX):
        return None
    try:
        return int(note[len(_PATH_NOTE_PREFIX):].split("|", 1)[0].strip())
    except (TypeError, ValueError):
        return None


def is_points_path(path) -> bool:
    """A POINTS path is an untagged any_path alternative whose flat item list
    carries per-item weights and a ``need`` points goal (``kind: "points"``) —
    the ``point_collection`` mode as an either-or branch."""
    return isinstance(path, dict) and path.get("kind") == "points"


def path_point_weights(path: dict) -> dict:
    """``{normalized item name -> integer point weight (>=1)}`` for a points
    path (``items: [{item_name, points}]``). Bare-string items weigh 1."""
    out: dict = {}
    for it in (path.get("items") or []):
        if isinstance(it, dict):
            name = norm(it.get("item_name") or it.get("name"))
            if not name:
                continue
            try:
                weight = int(round(float(it.get("points") or 1)))
            except (TypeError, ValueError):
                weight = 1
            out[name] = max(weight, 1)
        elif isinstance(it, str):
            name = norm(it)
            if name:
                out[name] = 1
    return out


def points_fold(rows, weights: dict) -> int:
    """Weighted point total of untagged item rows — each row's matched-item
    weight times its quantity (mirrors ``point_collection``). Wildcard (no
    matched item) and coin-bonus rows contribute nothing here."""
    total = 0
    for r in rows:
        if _is_coin_bonus(r):
            continue
        name = norm(getattr(r, "matched_target", None))
        if name and name in weights:
            total += weights[name] * _qty(r)
    return total


def parse_requirement_groups(config: dict) -> list:
    """``groups`` config -> normalized ``(mode, item-name set, need)`` tuples
    (shared by the grouped and any_path rollups)."""
    groups: list = []
    for group in (config.get("groups") or []):
        if not isinstance(group, dict):
            continue
        names = {
            norm(it if isinstance(it, str)
                 else (it or {}).get("item_name") or (it or {}).get("name"))
            for it in (group.get("items") or [])
        }
        names.discard("")
        if not names:
            continue
        mode = group.get("mode") if group.get("mode") in ("all_of", "any_of") else "all_of"
        try:
            need = max(int(group.get("need") or 0), 1) if mode == "any_of" else len(names)
        except (TypeError, ValueError):
            need = 1
        groups.append((mode, names, need))
    return groups


def count_progress_from_rows(rows, threshold: int) -> int:
    """Flat quantity sum, capped at ``threshold`` — the default rollup for
    single-target tasks (one item, loot value GP, a kill count)."""
    total = 0
    for r in rows:
        if _is_coin_bonus(r):
            continue
        total += _qty(r)
    return min(total, threshold)


def distinct_progress_from_rows(rows, threshold: int) -> int:
    """all_of/assembly rollup: one unit per DISTINCT listed item collected
    (quantity is irrelevant — a 1,338-coins drop is still just "Coins"), plus
    manual wildcard rows (no matched item) counting their quantity each,
    capped at the threshold so wildcard awards can't overshoot."""
    distinct: set = set()
    wildcard = 0
    for r in rows:
        if _is_coin_bonus(r):
            continue
        target = getattr(r, "matched_target", None)
        if target:
            distinct.add(norm(target))
        else:
            wildcard += _qty(r)
    return min(len(distinct) + wildcard, threshold)


def distinct_players_from_rows(rows, threshold: int) -> int:
    """One unit per DISTINCT contributing player (pb unique_players /
    whole_team rollups) — a grinder's tenth sub-threshold kill is still one
    player. Player-less manual wildcard rows count their quantity each."""
    players: set = set()
    wildcard = 0
    for r in rows:
        if _is_coin_bonus(r):
            continue
        pid = getattr(r, "player_id", None)
        if pid is not None:
            players.add(pid)
        else:
            wildcard += _qty(r)
    return min(len(players) + wildcard, threshold)


def grouped_progress_from_rows(rows, config: dict, threshold: int) -> int:
    """``kind: "groups"`` rollup — each group is its own all-of (distinct
    listed items) or any-of (quantities fold, capped at ``need``) requirement,
    and overall progress is the sum of per-group progress."""
    groups = parse_requirement_groups(config)

    distinct: list = [set() for _ in groups]
    folded = [0] * len(groups)
    wildcard = 0
    for r in rows:
        if _is_coin_bonus(r):
            continue
        name = norm(getattr(r, "matched_target", None))
        qty = _qty(r)
        if not name:
            wildcard += qty
            continue
        for gi, (mode, names, _need) in enumerate(groups):
            if name in names:
                if mode == "all_of":
                    distinct[gi].add(name)
                else:
                    folded[gi] += qty
                break

    progress = wildcard
    for gi, (mode, _names, need) in enumerate(groups):
        got = len(distinct[gi]) if mode == "all_of" else folded[gi]
        progress += min(got, need)
    return min(progress, threshold)


def anypath_progress_from_rows(rows, config: dict, threshold: int) -> int:
    """``kind: "any_path"`` rollup — the task completes when ANY path is fully
    satisfied ("the full Justiciar set OR any 5 Justiciar items").

    Paths differ in size, so the single rollup integer is the *percentage* of
    the closest-to-done path scaled to the threshold (validation pins
    target_value to 100). Floor rounding means the threshold is hit exactly
    when some path's own need is fully met, never one drop early.

    Metric paths (``{"metric": "kc"|"loot_value", "need": N}``) fold the
    quantities of their own tagged rows (``note`` = ``path:<idx>``); item and
    points paths fold only the untagged rows — so a 5,000-KC path can never
    leak kills into a sibling item checklist, or vice versa. Untagged WILDCARD
    rows (manual awards, no matched item) count on the task's percent scale.
    """
    item_rows = []
    tagged: dict = {}
    wildcard = 0
    for r in rows:
        idx = row_path_idx(r)
        if idx is None:
            item_rows.append(r)
            if not _is_coin_bonus(r) and not norm(getattr(r, "matched_target", None)):
                wildcard += _qty(r)
        elif not _is_coin_bonus(r):
            tagged[idx] = tagged.get(idx, 0) + _qty(r)
    best = 0
    for pi, path in enumerate(config.get("paths") or []):
        if not isinstance(path, dict):
            continue
        if path.get("metric") in PATH_METRICS:
            need = path_need(path)
            got = min(tagged.get(pi, 0), need)
            pct = min((got * threshold) // need + wildcard, threshold)
        elif is_points_path(path):
            need = path_need(path)
            got = min(points_fold(item_rows, path_point_weights(path)), need)
            pct = min((got * threshold) // need + wildcard, threshold)
        else:
            need = sum(n for _mode, _names, n in parse_requirement_groups(path))
            if need <= 0:
                continue
            got = grouped_progress_from_rows(item_rows, path, need)
            pct = (got * threshold) // need
        best = max(best, pct)
    return min(best, threshold)


def progress_from_rows(rows, progress_kind: str, config: dict, threshold: int) -> int:
    """Dispatch to the fold a config's shape calls for.

    ``progress_kind`` is derived ONCE at write time (the competition validator
    stores it on the bonus rule) rather than re-sniffed from the config at
    every fold, so a scorer never has to know the task-type vocabulary — and a
    config whose shape drifts can't silently change how history was counted.
    Unknown kinds fall back to the flat count, which is the shape every
    single-target task already uses.
    """
    config = config if isinstance(config, dict) else {}
    if progress_kind == "distinct":
        return distinct_progress_from_rows(rows, threshold)
    if progress_kind == "groups":
        return grouped_progress_from_rows(rows, config, threshold)
    if progress_kind == "any_path":
        return anypath_progress_from_rows(rows, config, threshold)
    if progress_kind == "points":
        # The rows ALREADY carry points, so summing them IS the points total.
        # ``event_engine.item_match_quantity`` returns the item's point value
        # as the credit for a top-level ``point_collection`` config, so
        # re-applying the weights here would square them: a 300-point drop
        # against a 500-point goal would fold to 90,000 and max the rule on its
        # first item. (An ``any_path`` POINTS path is different — its rows are
        # NOT pre-weighted, which is why ``anypath_progress_from_rows`` still
        # folds through ``points_fold``.)
        return count_progress_from_rows(rows, threshold)
    return count_progress_from_rows(rows, threshold)
