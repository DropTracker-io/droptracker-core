#!/usr/bin/env python3
"""Randomize event teams from the monthly loot leaderboard.

Selects every player who has received at least ``--threshold`` GP this month
(the ``leaderboard:{YYYYMM}`` Redis sorted set) and distributes them, balanced
and randomly, across an event's teams by writing ``web_event_team_members``
rows. Built for "Global Bingo #1" (event 9) but event-agnostic.

DRY RUN BY DEFAULT: prints exactly what would happen and writes nothing. Pass
``--apply`` to commit. The shuffle is seeded (default: the event id) so the
dry-run preview is byte-for-byte identical to the applied result.

Why this is safe/meaningful (verified against services/event_engine.py):
  * The matcher credits players purely from ``web_event_team_members`` — there
    is NO group-membership filter for a standard event, so placing players who
    are not in the owning group still counts their drops.
  * ``joined_at`` is the per-player credit cutoff: the engine ignores each
    player's submissions timestamped before it. The event is mid-run, so by
    default new members only score from placement time forward. ``--backdate``
    sets ``joined_at`` to the event's ``starts_at`` so the whole event window
    counts.

Examples:
  # Dry run (recommended first) — the default:
  venv/bin/python scripts/randomize_bingo_teams.py

  # Same, but only players linked to a Discord/user account, one account/user:
  venv/bin/python scripts/randomize_bingo_teams.py --linked-only --dedupe-user

  # Commit, counting the whole event window:
  venv/bin/python scripts/randomize_bingo_teams.py --backdate --apply
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy import func  # noqa: E402

from db import Session  # noqa: E402
from db.models import (  # noqa: E402
    AuditLog,
    Event,
    EventTeam,
    EventTeamMember,
    Player,
)
from utils.redis import RedisClient  # noqa: E402
from web_api.common import get_current_partition, leaderboard_key  # noqa: E402

DEFAULT_EVENT_NAME = "Global Bingo #1"
DEFAULT_THRESHOLD = 50_000_000


def _fmt(n: int) -> str:
    return f"{n:,}"


def resolve_event(s, args) -> Event:
    if args.event_id is not None:
        ev = s.query(Event).filter(Event.id == args.event_id).first()
        if not ev:
            sys.exit(f"!! No event with id={args.event_id}")
        return ev
    ev = s.query(Event).filter(Event.name == args.event_name).first()
    if not ev:
        sys.exit(f"!! No event named {args.event_name!r} (pass --event-id)")
    return ev


def select_candidates(s, rc, partition: int, threshold: int,
                      linked_only: bool, dedupe_user: bool):
    """Return (final_pids, stats). Ordered by month GP desc for reporting."""
    key = leaderboard_key(partition)
    raw = rc.zrangebyscore(key, threshold, "+inf", withscores=True)
    scored: list[tuple[int, int]] = []
    for member, score in raw:
        try:
            scored.append((int(member), int(score)))
        except (TypeError, ValueError):
            continue
    scored.sort(key=lambda x: -x[1])

    pids = [p for p, _ in scored]
    players = {}
    if pids:
        for pl in s.query(Player).filter(Player.player_id.in_(pids)).all():
            players[pl.player_id] = pl

    n_raw = len(scored)
    present = [(p, sc) for p, sc in scored if p in players]
    n_missing = n_raw - len(present)

    if linked_only:
        present = [(p, sc) for p, sc in present
                   if getattr(players[p], "user_id", None)]

    n_dropped_dupe = 0
    if dedupe_user:
        # Keep the top-earning account per user; accounts with no user stand
        # alone. `present` is already GP-desc, so the first seen per user wins.
        seen_users: set[int] = set()
        deduped: list[tuple[int, int]] = []
        for p, sc in present:
            uid = getattr(players[p], "user_id", None)
            if uid is not None:
                if uid in seen_users:
                    n_dropped_dupe += 1
                    continue
                seen_users.add(uid)
            deduped.append((p, sc))
        present = deduped

    stats = {
        "key": key,
        "zcard": rc.zcard(key),
        "raw": n_raw,
        "missing": n_missing,
        "linked_only": linked_only,
        "dedupe_user": dedupe_user,
        "dropped_dupe": n_dropped_dupe,
        "final": len(present),
    }
    return present, players, stats


def plan_assignment(present, team_ids, seed: int):
    """Balanced round-robin over a seeded shuffle. Deterministic given seed, so
    the dry run equals the apply run. Returns {team_id: [player_id, ...]}."""
    pids = [p for p, _ in present]
    rng = random.Random(seed)
    rng.shuffle(pids)
    buckets: dict[int, list[int]] = {tid: [] for tid in team_ids}
    n = len(team_ids)
    for i, pid in enumerate(pids):
        buckets[team_ids[i % n]].append(pid)
    return buckets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event-id", type=int, default=None)
    ap.add_argument("--event-name", default=DEFAULT_EVENT_NAME)
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help="min GP received this month (default 50,000,000)")
    ap.add_argument("--partition", type=int, default=None,
                    help="YYYYMM leaderboard partition (default: current month)")
    ap.add_argument("--team-ids", default=None,
                    help="comma-separated team ids to use (default: every team "
                         "on the event, id-ascending)")
    ap.add_argument("--additive", action="store_true",
                    help="keep current members; only (re)place candidates "
                         "(default: reset — clear all rosters first)")
    ap.add_argument("--linked-only", action="store_true",
                    help="only players linked to a user account")
    ap.add_argument("--dedupe-user", action="store_true",
                    help="one account per user (keep highest-earning)")
    ap.add_argument("--backdate", action="store_true",
                    help="set joined_at to the event start (count the whole "
                         "event window) instead of now")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed (default: the event id). Dry-run==apply for "
                         "a fixed seed.")
    ap.add_argument("--actor-user-id", type=int, default=None,
                    help="user_id recorded on the audit-log row")
    ap.add_argument("--apply", action="store_true",
                    help="COMMIT the assignment (default: dry run, no writes)")
    args = ap.parse_args()

    partition = args.partition or get_current_partition()
    s = Session()
    rc = RedisClient().client
    if rc is None:
        sys.exit("!! Redis unavailable")

    ev = resolve_event(s, args)
    seed = args.seed if args.seed is not None else ev.id

    # Teams ------------------------------------------------------------------
    teams = (s.query(EventTeam)
             .filter(EventTeam.event_id == ev.id)
             .order_by(EventTeam.id.asc()).all())
    if args.team_ids:
        wanted = [int(x) for x in args.team_ids.split(",") if x.strip()]
        teams = [t for t in teams if t.id in wanted]
    if not teams:
        sys.exit("!! Event has no usable teams (create teams first or pass "
                 "--team-ids)")
    team_ids = [t.id for t in teams]
    team_name = {t.id: t.name for t in teams}

    if any(getattr(t, "auto_clan", False) for t in teams):
        print("!! WARNING: one or more target teams is an auto_clan (whole-clan) "
              "team; explicit roster rows are ignored for those. Aborting.")
        sys.exit(1)

    # Candidates -------------------------------------------------------------
    present, players, stats = select_candidates(
        s, rc, partition, args.threshold, args.linked_only, args.dedupe_user)

    # Current membership (for reset-drop reporting) --------------------------
    current = (s.query(EventTeamMember.player_id, EventTeamMember.team_id)
               .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
               .filter(EventTeam.event_id == ev.id).all())
    current_pids = {p for p, _ in current}
    final_set = {p for p, _ in present}

    buckets = plan_assignment(present, team_ids, seed)
    joined_at = ev.starts_at if (args.backdate and ev.starts_at) else datetime.now()

    # ---------------------------------------------------------------- report
    mode = "ADDITIVE" if args.additive else "RESET"
    print("=" * 72)
    print(f"Event {ev.id}: {ev.name!r}  status={ev.status} mode={ev.mode} "
          f"group_id={ev.group_id}")
    print(f"  window: {ev.starts_at} -> {ev.ends_at}")
    print(f"  placement mode: {mode}   backdate joined_at: {args.backdate} "
          f"-> {joined_at}")
    print(f"  seed: {seed}   {'*** APPLY ***' if args.apply else 'DRY RUN (no writes)'}")
    print("-" * 72)
    print(f"Leaderboard {stats['key']}  (players on board: {stats['zcard']})")
    print(f"  >= {_fmt(args.threshold)} GP this month : {stats['raw']}")
    print(f"  minus stale/missing players           : -{stats['missing']}")
    if stats["linked_only"]:
        print("  minus unlinked (‑‑linked-only)          : applied")
    if stats["dedupe_user"]:
        print(f"  minus duplicate accounts (‑‑dedupe-user): -{stats['dropped_dupe']}")
    print(f"  FINAL candidates to place             : {stats['final']}")
    print("-" * 72)
    base = 0 if not args.additive else None
    print(f"Teams ({len(team_ids)}) — resulting sizes:")
    for tid in team_ids:
        add = len(buckets[tid])
        cur = sum(1 for p, t in current if t == tid)
        if args.additive:
            # additive keeps existing members not in the candidate set
            keep = sum(1 for p, t in current if t == tid and p not in final_set)
            print(f"  team {tid:>3} {team_name[tid]!r:16} "
                  f"keep {keep} + new {add} = {keep + add}")
        else:
            print(f"  team {tid:>3} {team_name[tid]!r:16} "
                  f"was {cur} -> {add}")

    if not args.additive:
        dropped = current_pids - final_set
        if dropped:
            print(f"\n  RESET drops {len(dropped)} current member(s) not in the "
                  f"candidate set:")
            for p in sorted(dropped):
                nm = getattr(players.get(p), "player_name", None)
                if nm is None:
                    pl = s.query(Player.player_name).filter(
                        Player.player_id == p).first()
                    nm = pl[0] if pl else "?"
                print(f"     - player_id={p} {nm!r}")

    # sample of the assignment
    print("\nSample assignment (first 3 per team):")
    for tid in team_ids:
        sample = buckets[tid][:3]
        names = [f"{players[p].player_name}({p})" for p in sample]
        print(f"  {team_name[tid]!r}: " + ", ".join(names) +
              (" ..." if len(buckets[tid]) > 3 else ""))

    total_assigned = sum(len(v) for v in buckets.values())
    print("-" * 72)
    print(f"Total players to place: {total_assigned} across {len(team_ids)} teams")

    if not args.apply:
        print("\nDRY RUN complete — nothing was written. Re-run with --apply to commit.")
        s.close()
        return

    # ------------------------------------------------------------------- apply
    from services.event_engine import publish_event_admin_bump

    try:
        if not args.additive:
            # Clear every roster row on this event's teams.
            ids = [t.id for t in s.query(EventTeam.id)
                   .filter(EventTeam.event_id == ev.id).all()]
            s.query(EventTeamMember).filter(
                EventTeamMember.team_id.in_(ids)
            ).delete(synchronize_session=False)
            s.flush()
        else:
            # Additive: remove only the candidates' existing membership on this
            # event (so a re-placed player doesn't duplicate), keep everyone else.
            s.query(EventTeamMember).filter(
                EventTeamMember.player_id.in_(list(final_set)),
                EventTeamMember.team_id.in_(team_ids),
            ).delete(synchronize_session=False)
            s.flush()

        for tid, pids in buckets.items():
            for pid in pids:
                s.add(EventTeamMember(team_id=tid, player_id=pid,
                                      joined_at=joined_at))

        s.add(AuditLog(
            actor_user_id=args.actor_user_id,
            group_id=ev.group_id,
            action="event.team.bulk_randomize",
            target=f"web_events.{ev.id}",
            before=f"members:{len(current_pids)}",
            after=(f"mode:{mode} threshold:{args.threshold} "
                   f"assigned:{total_assigned} teams:{team_ids} seed:{seed}"),
        ))
        s.commit()
    except Exception:
        s.rollback()
        raise

    publish_event_admin_bump(ev.id)
    print(f"\nAPPLIED: placed {total_assigned} players across {len(team_ids)} "
          f"teams; events worker bumped (rt:event-admin).")
    s.close()


if __name__ == "__main__":
    main()
