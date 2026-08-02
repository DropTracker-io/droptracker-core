"""Fold data stored against an ALIAS npc_list row onto its canonical row.

Why this exists
---------------
Several encounters are named two different ways by two different sources: the
plugin's chat-PB path reports the boss NPC it saw ("Corrupted Hunllef"), its
loot path reports the activity ("The Corrupted Gauntlet"), and the adventure
log reports a third short form ("Corrupted Gauntlet"). ``npc_list`` carries
rows for BOTH spellings, so the intake exact-name lookup happily stored the
same player's PBs under two npc_ids and every Gauntlet PB board (Hall of Fame,
site boss pages, ``/personal_bests``) showed up twice.

Intake now rewrites those names up front (``utils/npc_names``
``ENCOUNTER_NAME_ALIASES``) and prefers the canonical spelling when resolving
by slug, so no NEW rows land on the alias ids. This script heals the history
already written.

Scope is deliberately narrow: only npc_list rows whose slug is an ALIAS of
another row's match key (``npc_match_variants`` minus ``npc_primary_variants``)
are merged. Same-name duplicate rows — 116 "Guard"s, 85 "Zombie"s, the per-
variant ids of one monster — are NOT touched; they are distinct game NPCs that
happen to share a display name.

Per alias id it moves:
  * ``personal_best``    — merged per (player_id, team_size), keeping the
                           FASTER time (with the row that owns it: kill_time,
                           image/video, date, guid); surplus rows deleted.
  * ``drops``            — npc_id repointed.
  * ``player_npc_hourly_totals`` — repointed, summing into the canonical
                           bucket when (player, hour, partition) collides.
  * any other ``npc_id`` column in the schema — repointed, reported per table.
  * Redis per-NPC boards (``leaderboard:npc:{id}[:{part}]`` and their
                           ``leaderboard:group:{gid}:npc:{id}`` twins) — folded
                           into the canonical key with ZUNIONSTORE, TTL kept.
                           Rebuildable per-NPC caches on the alias id are
                           dropped. Skip with ``--skip-redis``.

The alias ``npc_list`` rows themselves are KEPT: they are real game NPC ids
used for icons and drop-source lookups.

Usage
-----
    python scripts/merge_alias_npc_ids.py            # dry run (default)
    python scripts/merge_alias_npc_ids.py --apply    # write
    python scripts/merge_alias_npc_ids.py --apply --only gauntlet
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from utils.npc_names import (  # noqa: E402
    ENCOUNTER_NAME_ALIASES,
    NPC_ALIASES,
    npc_match_key,
    npc_match_variants,
    npc_primary_rank_sql_expr,
    npc_primary_variants,
    npc_slug_sql_expr,
)

# Tables whose npc_id is merged by dedicated logic below; everything else with
# an npc_id column gets a plain repoint.
SPECIAL_TABLES = {"personal_best", "player_npc_hourly_totals", "npc_list"}


def alias_keys() -> list[str]:
    """Every canonical match key that has at least one alias spelling."""
    keys = set(NPC_ALIASES.values())
    keys.update(npc_match_key(v) for v in ENCOUNTER_NAME_ALIASES.values())
    return sorted(k for k in keys if k)


def resolve_group(s, key: str):
    """(canonical_id, canonical_name, [(alias_id, alias_name), …]) for a key."""
    variants = npc_match_variants(key)
    primary = npc_primary_variants(key)
    rows = s.execute(
        text(
            f"SELECT n.npc_id, n.npc_name, "
            f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
            f"              WHERE t.npc_id = n.npc_id) AS tracked "
            f"FROM npc_list n WHERE {npc_slug_sql_expr('n.npc_name')} IN :variants "
            f"ORDER BY {npc_primary_rank_sql_expr('n.npc_name')} ASC, "
            f"         tracked DESC, n.npc_id ASC"
        ).bindparams(
            bindparam("variants", expanding=True),
            bindparam("primary_variants", expanding=True),
        ),
        {"variants": variants, "primary_variants": primary},
    ).fetchall()
    if not rows:
        return None, None, []
    # Same ordering the intake resolvers use, so the merge target IS the row
    # new submissions now land on.
    canonical_id, canonical_name = int(rows[0][0]), rows[0][1]
    primary_set = set(primary)
    aliases = [
        (int(r[0]), r[1])
        for r in rows[1:]
        if _slug(r[1]) not in primary_set
    ]
    return canonical_id, canonical_name, aliases


def _slug(name: str) -> str:
    from utils.npc_names import npc_slug

    return npc_slug(name)


def merge_personal_bests(s, canonical_id: int, alias_ids: list[int], dry_run: bool) -> dict:
    """Fold alias PB rows onto the canonical (player, team_size) board."""
    rows = s.execute(
        text(
            "SELECT id, player_id, team_size, personal_best, kill_time, image_url, "
            "       video_url, date_added, used_api, unique_id "
            "FROM personal_best WHERE npc_id IN :aliases "
            "ORDER BY player_id, team_size, personal_best ASC"
        ).bindparams(bindparam("aliases", expanding=True)),
        {"aliases": alias_ids},
    ).fetchall()
    stats = {"moved": 0, "improved": 0, "dropped": 0, "rows": len(rows)}
    for r in rows:
        (row_id, player_id, team_size, pb, kill_time, image_url,
         video_url, date_added, used_api, unique_id) = r
        existing = s.execute(
            text(
                "SELECT id, personal_best FROM personal_best "
                "WHERE npc_id = :npc AND player_id = :pid AND team_size = :ts "
                "ORDER BY personal_best ASC LIMIT 1"
            ),
            {"npc": canonical_id, "pid": player_id, "ts": team_size},
        ).fetchone()
        if not existing:
            stats["moved"] += 1
            if not dry_run:
                s.execute(
                    text("UPDATE personal_best SET npc_id = :npc WHERE id = :id"),
                    {"npc": canonical_id, "id": row_id},
                )
            continue
        keep_id, keep_pb = int(existing[0]), existing[1]
        # A stored 0/NULL means "no time recorded", not an instant kill.
        alias_better = pb and pb > 0 and (not keep_pb or keep_pb <= 0 or pb < keep_pb)
        if alias_better:
            stats["improved"] += 1
            if not dry_run:
                s.execute(
                    text(
                        "UPDATE personal_best SET personal_best = :pb, kill_time = :kt, "
                        "       image_url = :img, video_url = :vid, date_added = :da, "
                        "       used_api = :api, unique_id = :guid "
                        "WHERE id = :id"
                    ),
                    {"pb": pb, "kt": kill_time, "img": image_url, "vid": video_url,
                     "da": date_added, "api": used_api, "guid": unique_id, "id": keep_id},
                )
        stats["dropped"] += 1
        if not dry_run:
            s.execute(text("DELETE FROM personal_best WHERE id = :id"), {"id": row_id})
    return stats


def merge_hourly_totals(s, canonical_id: int, alias_ids: list[int], dry_run: bool) -> dict:
    """Repoint hourly buckets, summing into the canonical bucket on collision
    (the table's unique key is (player_id, npc_id, date_hour, partition))."""
    rows = s.execute(
        text(
            "SELECT id, player_id, date_hour, `partition`, total_value, drop_count, "
            "       last_drop_time FROM player_npc_hourly_totals WHERE npc_id IN :aliases"
        ).bindparams(bindparam("aliases", expanding=True)),
        {"aliases": alias_ids},
    ).fetchall()
    stats = {"moved": 0, "summed": 0, "rows": len(rows)}
    for row_id, player_id, date_hour, part, total_value, drop_count, last_drop in rows:
        existing = s.execute(
            text(
                "SELECT id FROM player_npc_hourly_totals WHERE npc_id = :npc "
                "AND player_id = :pid AND date_hour = :dh AND `partition` = :part"
            ),
            {"npc": canonical_id, "pid": player_id, "dh": date_hour, "part": part},
        ).fetchone()
        if existing:
            stats["summed"] += 1
            if not dry_run:
                s.execute(
                    text(
                        "UPDATE player_npc_hourly_totals "
                        "SET total_value = COALESCE(total_value, 0) + :tv, "
                        "    drop_count = COALESCE(drop_count, 0) + :dc, "
                        "    last_drop_time = GREATEST(COALESCE(last_drop_time, :ldt), :ldt) "
                        "WHERE id = :id"
                    ),
                    {"tv": total_value or 0, "dc": drop_count or 0,
                     "ldt": last_drop, "id": int(existing[0])},
                )
                s.execute(
                    text("DELETE FROM player_npc_hourly_totals WHERE id = :id"), {"id": row_id}
                )
        else:
            stats["moved"] += 1
            if not dry_run:
                s.execute(
                    text("UPDATE player_npc_hourly_totals SET npc_id = :npc WHERE id = :id"),
                    {"npc": canonical_id, "id": row_id},
                )
    return stats


def merge_redis_npc_keys(canonical_id: int, alias_ids: list[int], dry_run: bool) -> dict:
    """Fold the alias ids' per-NPC Redis structures onto the canonical id.

    Sorted sets (the loot boards, global and per-group, all-time and monthly)
    are ZUNIONSTOREd into their canonical twin — which is additive, exactly
    like the intake ZINCRBYs that built them — then deleted. ZUNIONSTORE
    recreates the destination, so a monthly key's TTL is re-applied after.
    Anything else on the alias id is a rebuildable cache and is just dropped.
    """
    from utils.redis import redis_client

    client = redis_client.client
    stats = {"folded": 0, "dropped": 0, "skipped": 0}
    for alias_id in alias_ids:
        for key in list(client.scan_iter(match=f"*npc:{alias_id}", count=1000)) + list(
            client.scan_iter(match=f"*npc:{alias_id}:*", count=1000)
        ):
            name = key.decode() if isinstance(key, bytes) else key
            # Guard the glob: "*npc:902*" must not sweep up npc:9021 when
            # merging npc:902. scan_iter patterns above are exact-suffix or
            # exact-token, but re-check the token to be certain.
            if f"npc:{alias_id}" not in name:
                continue
            target = name.replace(f"npc:{alias_id}", f"npc:{canonical_id}")
            kind = client.type(key)
            kind = kind.decode() if isinstance(kind, bytes) else kind
            if kind == "zset":
                stats["folded"] += 1
                print(f"           redis fold {name} -> {target}")
                if not dry_run:
                    ttl = client.ttl(target)
                    client.zunionstore(target, [target, name])
                    if ttl and ttl > 0:
                        client.expire(target, ttl)
                    client.delete(name)
            elif kind in ("hash", "string", "list", "set"):
                stats["dropped"] += 1
                print(f"           redis drop {name} ({kind}, rebuildable cache)")
                if not dry_run:
                    client.delete(name)
            else:
                stats["skipped"] += 1
                print(f"           redis SKIP {name} (type {kind})")
    return stats


def other_npc_tables(s) -> list[str]:
    rows = s.execute(
        text(
            "SELECT TABLE_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'npc_id' "
            "ORDER BY TABLE_NAME"
        )
    ).fetchall()
    return [r[0] for r in rows if r[0] not in SPECIAL_TABLES]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fold alias npc_list rows' data onto their canonical row."
    )
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--only", help="restrict to one canonical match key, e.g. 'gauntlet'")
    ap.add_argument("--skip-redis", action="store_true",
                    help="leave the per-NPC Redis boards alone (DB rows only)")
    args = ap.parse_args()
    dry_run = not args.apply
    tag = "[merge_alias_npc_ids]"
    print(f"{tag} {'DRY-RUN — no writes' if dry_run else 'APPLYING'}")

    from db.models.base import Session

    s = Session()
    try:
        tables = other_npc_tables(s)
        keys = [k for k in alias_keys() if not args.only or k == args.only]
        if args.only and not keys:
            print(f"{tag} no alias group matches key {args.only!r}")
            return 1
        total_pb = 0
        pending_redis: list[tuple[int, list[int]]] = []
        for key in keys:
            canonical_id, canonical_name, aliases = resolve_group(s, key)
            if canonical_id is None:
                print(f"{tag} {key}: no npc_list row — skipped")
                continue
            if not aliases:
                print(f"{tag} {key}: canonical {canonical_id} ({canonical_name}), no alias rows")
                continue
            alias_ids = [a for a, _ in aliases]
            print(f"{tag} {key}: canonical {canonical_id} ({canonical_name}) <- "
                  + ", ".join(f"{i} ({n})" for i, n in aliases))

            pb = merge_personal_bests(s, canonical_id, alias_ids, dry_run)
            total_pb += pb["rows"]
            print(f"{tag}   personal_best: {pb['rows']} alias row(s) -> "
                  f"{pb['moved']} repointed, {pb['improved']} improved an existing board, "
                  f"{pb['dropped']} folded away")

            hourly = merge_hourly_totals(s, canonical_id, alias_ids, dry_run)
            if hourly["rows"]:
                print(f"{tag}   player_npc_hourly_totals: {hourly['rows']} row(s) -> "
                      f"{hourly['moved']} repointed, {hourly['summed']} summed into existing")

            for table in tables:
                count = s.execute(
                    text(f"SELECT COUNT(*) FROM `{table}` WHERE npc_id IN :aliases")
                    .bindparams(bindparam("aliases", expanding=True)),
                    {"aliases": alias_ids},
                ).scalar()
                if not count:
                    continue
                print(f"{tag}   {table}: {count} row(s) repointed")
                if not dry_run:
                    s.execute(
                        text(f"UPDATE `{table}` SET npc_id = :npc WHERE npc_id IN :aliases")
                        .bindparams(bindparam("aliases", expanding=True)),
                        {"npc": canonical_id, "aliases": alias_ids},
                    )

            # Redis is not transactional with MySQL: queue the fold and run it
            # only once the row moves commit, so a failed transaction can't
            # leave Redis credited to the canonical id while the rows sit on
            # the alias.
            if not args.skip_redis:
                pending_redis.append((canonical_id, alias_ids))
        if dry_run:
            s.rollback()
        else:
            s.commit()
            print(f"{tag} committed {total_pb} PB row move(s)")
        for canonical_id, alias_ids in pending_redis:
            merge_redis_npc_keys(canonical_id, alias_ids, dry_run)
        if dry_run:
            print(f"{tag} dry run complete ({total_pb} PB row(s) would move) — "
                  f"re-run with --apply to write")
        else:
            print(f"{tag} done")
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
