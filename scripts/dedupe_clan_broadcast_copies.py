"""Remove clan-broadcast drop copies that double-count a plugin drop (ticket #491).

Why this exists (reported 2026-10-19)
-------------------------------------
A clan broadcast about a plugin user is held for five minutes, then recorded
as a ``source='clan_chat'`` drop only if the player's own plugin submission
didn't arrive (``data/submissions/clan_broadcast._plugin_copy_exists``). That
check compared ``item_id`` exactly, but a broadcast only names the item, and
``ensure_item_by_name`` returns whichever row with that name comes first.
Noted drops broke it: the plugin stores Runite ore as 452 (noted), the name
lookup returns 451, the check missed, and the chat copy was recorded and
posted a second time. Fixed forward in the same commit as this script (the
check now matches every id sharing the name). This script cleans up what is
already on disk.

What it does
------------
1. Walks every ``clan_chat`` drop through ``ix_drops_unique_id`` (chat guids
   are ``cc:<sha256>``, so this is an index prefix range, not a table scan).
2. For each, looks for a non-``clan_chat`` row of the same player, the same
   item NAME (any id in the family) and the same quantity, within
   ``--window`` seconds (default 600, the server's own look-back). Pairing is
   one-to-one: a plugin row absorbs at most one chat copy, so two real drops
   and one broadcast never cost a real drop.
3. Keeps the plugin row (it carries the kill count and the screenshot) and
   deletes the chat copy with its ``notified``, ``group_recent_drops`` and
   automatic ``drop_group_moderation`` 'excluded' rows.
4. Recomputes the affected ``player_npc_hourly_totals`` /
   ``player_item_hourly_totals`` buckets from ``drops``, rebuilds the Redis
   loot leaderboards for each affected player, and repairs their per-NPC
   boards (the same repairs ``scripts/remove_bogus_drop.py`` makes).

What chat copies never had
--------------------------
The chat path awards no clan points, no event credit and no GP splits (see
``_process_chat_drop``). A copy that nevertheless has a ``player_points``,
``web_event_completions``, ``drop_splits``, ``video_uploads`` or a
human-reviewed ``drop_group_moderation`` row is left alone and listed.

Discord messages
----------------
The duplicate posts stay up by default; their channel/message ids are in the
snapshot. ``--delete-discord-messages`` removes them through the REST API
(no gateway needed).

Safety
------
  * Dry-run by default; ``--apply`` is required to write anything.
  * Every doomed row (drop, notified, group_recent_drops) is snapshotted to
    ``logs/dedupe_clan_broadcast_copies_<ts>.json`` before anything is deleted.
  * Deletes in small per-chunk transactions; intake keeps writing.
  * Idempotent: a second run finds nothing.
  * Main-world ``drops`` only (the chat path never writes seasonal drops).

Run (dry-run):
    cd /store/droptracker/disc && ./venv/bin/python -m scripts.dedupe_clan_broadcast_copies
Then, once the numbers look right:
    cd /store/droptracker/disc && ./venv/bin/python -m scripts.dedupe_clan_broadcast_copies --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "logs")
STATEMENT_SECONDS = 20
PAGE = 2000
CHUNK = 200

#: References a chat copy should never have. One present means the row is not
#: what this script thinks it is: report it, never cascade.
BLOCKING_REFS = (
    ("player_points", "entry_id", "AND reason = 'drop'"),
    ("web_event_completions", "source_id", "AND source_type = 'drop'"),
    ("drop_splits", "drop_id", ""),
    # A human review is a decision to keep; the chat path's own automatic
    # 'excluded' rows (no reviewer) are cascaded below instead.
    ("drop_group_moderation", "drop_id", "AND reviewed_by_user_id IS NOT NULL"),
    ("video_uploads", "drop_id", ""),
)

#: Rows that exist only because the chat copy did; they go with it.
CASCADE_REFS = (
    ("notified", "drop_id"),
    ("group_recent_drops", "drop_id"),
    # Written by clan_broadcast._record_group_exclusions for non-opted groups.
    ("drop_group_moderation", "drop_id"),
)


# ── pure logic (unit-tested) ────────────────────────────────────────────────

def pair_copies(chat_rows, plugin_rows, window_seconds):
    """One-to-one pairing of chat copies with plugin rows.

    Both inputs are dicts with drop_id, player_id, name (item name, folded),
    quantity and date_added. A pair needs the same player, name and quantity
    and a gap of at most ``window_seconds``. Chat copies are taken in time
    order and each claims the nearest unclaimed plugin row, so a plugin row is
    never the twin of two copies. Returns [(chat_row, plugin_row)].
    """
    pool = defaultdict(list)
    for p in plugin_rows:
        pool[(p["player_id"], p["name"], p["quantity"])].append(p)
    claimed = set()
    pairs = []
    for c in sorted(chat_rows, key=lambda r: (r["date_added"], r["drop_id"])):
        best, best_gap = None, None
        for p in pool.get((c["player_id"], c["name"], c["quantity"]), ()):
            if p["drop_id"] in claimed:
                continue
            gap = abs((p["date_added"] - c["date_added"]).total_seconds())
            if gap <= window_seconds and (best_gap is None or gap < best_gap):
                best, best_gap = p, gap
        if best is not None:
            claimed.add(best["drop_id"])
            pairs.append((c, best))
    return pairs


def hour_bucket(dt):
    """The rollup tables' ``date_hour`` key (DATE_FORMAT '%Y-%m-%d-%H')."""
    return dt.strftime("%Y-%m-%d-%H")


# ── DB helpers ──────────────────────────────────────────────────────────────

def _q(s, sql, **params):
    from sqlalchemy import bindparam, text

    stmt = text(f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR " + sql.lstrip())
    expanding = [k for k, v in params.items() if isinstance(v, (list, tuple, set))]
    if expanding:
        stmt = stmt.bindparams(*[bindparam(k, expanding=True) for k in expanding])
        params = {k: (sorted(v) if k in expanding else v) for k, v in params.items()}
    return [dict(r) for r in s.execute(stmt, params).mappings()]


def _chat_rows(s, since):
    """Every clan_chat drop, keyset-paged over ix_drops_unique_id."""
    out, last = [], "cc:"
    while True:
        page = _q(s, """
            SELECT d.drop_id, d.player_id, d.item_id, d.npc_id, d.quantity, d.value,
                   d.date_added, d.`partition`, d.unique_id, i.item_name
            FROM drops d LEFT JOIN items i ON i.item_id = d.item_id
            WHERE d.unique_id > :last AND d.unique_id < 'cc;' AND d.source = 'clan_chat'
            ORDER BY d.unique_id LIMIT :lim""", last=last, lim=PAGE)
        if not page:
            return out
        last = page[-1]["unique_id"]
        out.extend(r for r in page if since is None or r["date_added"] >= since)
        time.sleep(0.02)


def _families(s, names):
    """item name (folded) -> every item id with that name."""
    fam = defaultdict(set)
    names = sorted({n for n in names if n})
    for i in range(0, len(names), 500):
        for r in _q(s, "SELECT item_id, item_name FROM items WHERE item_name IN :names",
                    names=names[i:i + 500]):
            fam[(r["item_name"] or "").lower()].add(int(r["item_id"]))
    return fam


def _plugin_candidates(s, chat_rows, families, window):
    """Non-clan_chat rows that could be a chat copy's twin: one short
    (player_id, date_added) index range per copy, deduped by drop_id."""
    seen = {}
    for c in chat_rows:
        ids = families.get((c["item_name"] or "").lower(), set()) | {int(c["item_id"])}
        for r in _q(s, """
                SELECT d.drop_id, d.player_id, d.item_id, d.quantity, d.value, d.date_added,
                       d.kill_count, d.unique_id, i.item_name
                FROM drops d LEFT JOIN items i ON i.item_id = d.item_id
                WHERE d.player_id = :p AND d.date_added >= :lo AND d.date_added <= :hi
                  AND d.quantity = :q AND (d.source IS NULL OR d.source <> 'clan_chat')
                  AND d.item_id IN :ids""",
                p=c["player_id"], q=c["quantity"], ids=ids,
                lo=c["date_added"] - timedelta(seconds=window),
                hi=c["date_added"] + timedelta(seconds=window)):
            seen[r["drop_id"]] = r
    return list(seen.values())


def _refs(s, drop_ids, tables):
    found = defaultdict(list)
    for spec in tables:
        table, col = spec[0], spec[1]
        extra = spec[2] if len(spec) > 2 else ""
        for i in range(0, len(drop_ids), 500):
            for r in _q(s, f"SELECT {col} AS ref, COUNT(*) AS n FROM {table} "
                           f"WHERE {col} IN :ids {extra} GROUP BY {col}",
                        ids=drop_ids[i:i + 500]):
                found[int(r["ref"])].append((table, int(r["n"])))
    return found


def _rows_of(s, table, col, drop_ids):
    out = []
    for i in range(0, len(drop_ids), 500):
        out.extend(_q(s, f"SELECT * FROM {table} WHERE {col} IN :ids", ids=drop_ids[i:i + 500]))
    return out


def _json_safe(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def _recompute_bucket(s, table, key_col, player_id, key_id, date_hour, ceiling):
    """Rewrite one hourly rollup bucket from ``drops`` (capped at the tailer
    pointer so the additive tailer never double-counts), or delete it when no
    drops remain."""
    from sqlalchemy import text

    start = datetime.strptime(date_hour, "%Y-%m-%d-%H")
    end = start + timedelta(hours=1)
    qty_sql = ", COALESCE(SUM(quantity), 0) AS qty" if table == "player_item_hourly_totals" else ""
    row = _q(s, f"""
        SELECT COUNT(*) AS n, COALESCE(SUM(value * quantity), 0) AS total,
               MAX(date_added) AS last {qty_sql}
        FROM drops WHERE player_id = :p AND {key_col} = :k
          AND date_added >= :a AND date_added < :b AND drop_id <= :ceil""",
        p=player_id, k=key_id, a=start, b=end, ceil=ceiling)[0]
    where = f"player_id = :p AND {key_col} = :k AND date_hour = :h"
    if not row["n"]:
        s.execute(text(f"DELETE FROM {table} WHERE {where}"), {"p": player_id, "k": key_id, "h": date_hour})
        return
    sets = "total_value = :total, drop_count = :n, last_drop_time = :last"
    params = {"p": player_id, "k": key_id, "h": date_hour, "total": int(row["total"]),
              "n": int(row["n"]), "last": row["last"]}
    if qty_sql:
        sets += ", quantity = :qty"
        params["qty"] = int(row["qty"])
    s.execute(text(f"UPDATE {table} SET {sets} WHERE {where}"), params)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--since", type=datetime.fromisoformat, default=None,
                    help="only chat copies on/after this date (default: all)")
    ap.add_argument("--window", type=int, default=600,
                    help="max seconds between a chat copy and its plugin twin (default 600)")
    ap.add_argument("--delete-discord-messages", action="store_true",
                    help="also delete the duplicate Discord posts (default: keep them)")
    ap.add_argument("--skip-redis", action="store_true", help="skip the Redis rebuild")
    args = ap.parse_args(argv)

    from sqlalchemy import text

    from db.models.base import Session

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== dedupe_clan_broadcast_copies [{mode}] since={args.since or 'all'} "
          f"window={args.window}s ===\n")
    s = Session()
    try:
        chat = _chat_rows(s, args.since)
        print(f"clan_chat drops scanned: {len(chat)}")
        if not chat:
            print("Nothing to do.")
            return 0
        families = _families(s, [c["item_name"] for c in chat])
        plugin = _plugin_candidates(s, chat, families, args.window)
        for r in chat + plugin:
            r["name"] = (r.get("item_name") or "").lower() or f"id:{r['item_id']}"
        pairs = pair_copies(chat, plugin, args.window)
        print(f"chat copies with a plugin twin: {len(pairs)}")
        if not pairs:
            print("Nothing to do.")
            return 0

        doomed_ids = [c["drop_id"] for c, _ in pairs]
        blocked = _refs(s, doomed_ids, BLOCKING_REFS)
        pairs_ok = [(c, p) for c, p in pairs if c["drop_id"] not in blocked]

        gaps = defaultdict(int)
        kinds = defaultdict(int)
        per_group_posts = defaultdict(int)
        for c, p in pairs_ok:
            gap = abs((p["date_added"] - c["date_added"]).total_seconds())
            gaps["<=10s" if gap <= 10 else "<=60s" if gap <= 60 else "<=5m" if gap <= 300 else ">5m"] += 1
            kinds["different id (the #491 bug)" if c["item_id"] != p["item_id"] else "same id"] += 1
        notified = _rows_of(s, "notified", "drop_id", [c["drop_id"] for c, _ in pairs_ok])
        recent = _rows_of(s, "group_recent_drops", "drop_id", [c["drop_id"] for c, _ in pairs_ok])
        moderation = _rows_of(s, "drop_group_moderation", "drop_id", [c["drop_id"] for c, _ in pairs_ok])
        for n in notified:
            per_group_posts[n["group_id"]] += 1
        surplus = sum(int(c["value"] or 0) * int(c["quantity"] or 0) for c, _ in pairs_ok)
        by_item = defaultdict(int)
        for c, _ in pairs_ok:
            by_item[c["item_name"]] += 1
        players = {c["player_id"] for c, _ in pairs_ok}

        print(f"  deletable: {len(pairs_ok)}   blocked: {len(blocked)}")
        print(f"  by cause: {dict(kinds)}")
        print(f"  gap to plugin twin: {dict(gaps)}")
        print(f"  inflated loot: {surplus:,} gp across {len(players)} player(s)")
        print(f"  duplicate Discord posts (notified rows): {len(notified)} in "
              f"{len(per_group_posts)} group(s): "
              + ", ".join(f"g{g}x{n}" for g, n in sorted(per_group_posts.items(), key=lambda kv: -kv[1])[:15]))
        print("  top items: " + ", ".join(f"{k} x{v}" for k, v in
                                          sorted(by_item.items(), key=lambda kv: -kv[1])[:15]))
        for pid, refs in sorted(blocked.items()):
            print(f"    ! BLOCKED drop {pid}: " + ", ".join(f"{t} x{n}" for t, n in refs))
        print("  sample pairs (chat -> plugin):")
        for c, p in pairs_ok[:10]:
            print(f"    {c['drop_id']} (id {c['item_id']}, {c['date_added']}) -> {p['drop_id']} "
                  f"(id {p['item_id']}, {p['date_added']}, kc {p.get('kill_count')}) "
                  f"player {c['player_id']} {c['quantity']} x {c['item_name']}")

        os.makedirs(LOG_DIR, exist_ok=True)
        snap = os.path.join(LOG_DIR, f"dedupe_clan_broadcast_copies_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(snap, "w") as fh:
            json.dump({
                "generated_at": datetime.now().isoformat(),
                "applied": args.apply,
                "window": args.window,
                "pairs": [{"chat": {k: _json_safe(v) for k, v in c.items()},
                           "plugin_twin": {k: _json_safe(v) for k, v in p.items()}}
                          for c, p in pairs_ok],
                "notified_deleted": [{k: _json_safe(v) for k, v in r.items()} for r in notified],
                "group_recent_drops_deleted": [{k: _json_safe(v) for k, v in r.items()} for r in recent],
                "drop_group_moderation_deleted": [{k: _json_safe(v) for k, v in r.items()} for r in moderation],
                "blocked": {str(k): v for k, v in blocked.items()},
            }, fh, indent=1, default=str)
        print(f"\nSnapshot written: {snap}")

        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply.")
            return 0

        s.rollback()
        ids = [c["drop_id"] for c, _ in pairs_ok]
        deleted = defaultdict(int)
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            from sqlalchemy import bindparam

            for table, col in (*CASCADE_REFS, ("drops", "drop_id")):
                stmt = text(f"DELETE FROM {table} WHERE {col} IN :ids").bindparams(
                    bindparam("ids", expanding=True))
                deleted[table] += s.execute(stmt, {"ids": chunk}).rowcount
            s.commit()
            print(f"  ...{min(i + CHUNK, len(ids))}/{len(ids)}")
            time.sleep(0.05)
        print("  deleted: " + ", ".join(f"{t} {n}" for t, n in deleted.items()))

        from services import item_totals, npc_totals

        buckets = 0
        for mod, table, key_col, key in ((npc_totals, "player_npc_hourly_totals", "npc_id", "npc_id"),
                                         (item_totals, "player_item_hourly_totals", "item_id", "item_id")):
            ceiling = mod.get_pointer()
            if ceiling is None:
                print(f"  ! {table}: no tailer pointer, rollup not repaired")
                continue
            todo = {(c["player_id"], int(c[key]), hour_bucket(c["date_added"]))
                    for c, _ in pairs_ok if c["drop_id"] <= ceiling}
            for pid, kid, dh in sorted(todo):
                _recompute_bucket(s, table, key_col, pid, kid, dh, ceiling)
                buckets += 1
            s.commit()
        print(f"  hourly rollup buckets recomputed: {buckets}")

        if not args.skip_redis:
            from scripts.remove_bogus_drop import _npc_board_keys, _true_npc_totals
            from services.redis_updates import force_update_player
            from utils.redis import RedisClient

            redis = RedisClient().client
            npc_parts = {(c["player_id"], int(c["npc_id"]), int(c["partition"])) for c, _ in pairs_ok
                         if c.get("npc_id") and c.get("partition")}
            groups_of = defaultdict(list)
            for r in _q(s, "SELECT DISTINCT player_id, group_id FROM user_group_association "
                           "WHERE player_id IN :p", p=list(players)):
                groups_of[r["player_id"]].append(int(r["group_id"]))
            failures = 0
            for pid in sorted(players):
                try:
                    force_update_player(pid, s)
                except Exception as exc:
                    failures += 1
                    print(f"  force_update_player({pid}) FAILED: {exc}", file=sys.stderr)
                time.sleep(0.05)
            for pid, nid, part in sorted(npc_parts):
                month, all_time = _true_npc_totals(s, pid, nid, part)
                for key in _npc_board_keys(nid, part, groups_of.get(pid, [])):
                    score = month if key.endswith(f":{part}") else all_time
                    if score > 0:
                        redis.zadd(key, {str(pid): score})
                    else:
                        redis.zrem(key, str(pid))
            s.rollback()
            print(f"  Redis rebuilt for {len(players) - failures}/{len(players)} player(s), "
                  f"{len(npc_parts)} NPC board set(s) repaired")

        if args.delete_discord_messages:
            from scripts.remove_bogus_drop import _delete_discord_message

            ok = 0
            for n in notified:
                if n.get("channel_id") and n.get("message_id") and \
                        _delete_discord_message(n["channel_id"], n["message_id"]):
                    ok += 1
                time.sleep(0.3)
            print(f"  Discord duplicate posts deleted: {ok}/{len(notified)}")
        print("\nDone.")
        return 0
    finally:
        s.rollback()
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
