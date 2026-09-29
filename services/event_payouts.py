"""Prize-pot payouts — who gets paid, and how much.

The reverse of the buy-in ledger (``services/event_prizes.py``): given the pot
(Σ paid buy-ins + donations), the event's distribution rule
(``prize_config.distribution``) and the final (or live) standings, work out
which teams/players finished in a paid place and what each member is owed.
Shown to event admins only (``GET /events/{id}/payouts``); nothing here moves
GP — payouts are still traded in-game, this is the checklist for doing it.

**Places.** ``first_only`` pays 100% to 1st; ``top_n`` splits the pot evenly
across the top N places; ``custom_split`` pays ``splits[i]`` percent to place
i+1. Placements come from the same rankers the clan-point awards use
(``services/event_point_awards``), so a board game ranks by the finish-line
race and a SOTW/BOTW individual race places players rather than its one roster
team. A team (or player) that scored nothing is never placed.

**Ties** share a place and the next place is skipped ("1, 1, 3"). Tied entries
pool the percentages of every place they jointly occupy and split that evenly —
two teams tied for 1st on a 60/30/10 split take 45% each and 3rd still gets 10%.

**Members.** A placed team's amount is split evenly across its roster (or,
with ``payout_active_only``, across the members who took part). Amounts are
whole GP, rounded down; what rounding leaves over is reported, never silently
handed to someone. A paid place nobody reached (two teams, top-3 split) is
reported as unclaimed.

The top half is pure and stdlib-only (loaded by file path in the unit tests);
everything touching the DB is lazy-imported below the divider.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Optional


# ══════════════════════════════════════════════════════════════════════════════
# Pure
# ══════════════════════════════════════════════════════════════════════════════

def place_shares(distribution: str, top_n: int, splits: Optional[list]) -> list:
    """Fraction of the pot each place earns, 1st place first."""
    if distribution == "top_n":
        n = max(1, int(top_n or 1))
        return [Fraction(1, n)] * n
    if distribution == "custom_split":
        clean = [int(s) for s in (splits or []) if isinstance(s, int) and s > 0]
        if clean and sum(clean) == 100:
            return [Fraction(s, 100) for s in clean]
        return [Fraction(1)]
    return [Fraction(1)]


def allocate(total: int, shares: list, placements: Iterable) -> dict:
    """Split ``total`` GP across placed entities.

    ``placements`` is ``[(entity, place)]`` (1-based, ties share a place).
    Returns ``{"entities": {entity: {"place", "tied", "share", "amount"}},
    "unclaimed", "unclaimed_places", "rounding"}``; ``share`` is the fraction
    of the pot that entity takes. ``amount`` + ``unclaimed`` + ``rounding``
    always sums to ``total``."""
    total = max(0, int(total or 0))
    by_place: dict = {}
    for entity, place in placements:
        if place and int(place) >= 1:
            by_place.setdefault(int(place), []).append(entity)

    out: dict = {}
    claimed: set = set()
    paid = 0
    for place in sorted(by_place):
        tied = by_place[place]
        k = len(tied)
        occupied = range(place - 1, place - 1 + k)
        claimed.update(occupied)
        pooled = sum((shares[i] for i in occupied if i < len(shares)), Fraction(0))
        each_share = pooled / k
        each = (total * each_share.numerator) // each_share.denominator
        for entity in tied:
            out[entity] = {"place": place, "tied": k > 1,
                           "share": each_share, "amount": int(each)}
            paid += int(each)

    open_places = [i + 1 for i in range(len(shares)) if i not in claimed]
    open_share = sum((shares[i - 1] for i in open_places), Fraction(0))
    unclaimed = (total * open_share.numerator) // open_share.denominator
    return {
        "entities": out,
        "unclaimed": int(unclaimed),
        "unclaimed_places": open_places,
        "rounding": total - paid - int(unclaimed),
    }


def split_evenly(amount: int, member_ids: list) -> tuple:
    """``({member: amount}, remainder)`` — whole GP each, rounded down."""
    amount = max(0, int(amount or 0))
    if not member_ids:
        return {}, amount
    each = amount // len(member_ids)
    return {m: each for m in member_ids}, amount - each * len(member_ids)


def share_percent(share) -> float:
    """A Fraction share as a percentage rounded for display (33.33)."""
    return round(float(share) * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
# DB-backed
# ══════════════════════════════════════════════════════════════════════════════

def _conquest_placements(session, event) -> tuple:
    """``({team_id: place}, {team_id: name})`` for Conquest — map score, then
    tiles, then regions held, exactly like the ended announcement."""
    from services.conquest_engine import conquest_final_standings
    from services.event_point_awards import competition_places

    rows = conquest_final_standings(session, event, limit=10_000)
    labels = {r["team_id"]: r["name"] for r in rows}
    scored = [r for r in rows
              if float(r.get("score") or 0) > 0 or int(r.get("tiles") or 0) > 0]
    places = competition_places([
        (r["team_id"], (round(float(r.get("score") or 0), 2),
                        int(r.get("tiles") or 0), int(r.get("regions") or 0)))
        for r in scored
    ])
    return dict(places), labels


def _team_rosters(session, event) -> dict:
    """``{team_id: [(player_id, player_name)]}``. A whole-clan team with no
    roster rows yet falls back to live clan membership (same rule as the
    team lootboards)."""
    from db.models import EventTeam, Player
    from lootboard.team_boards import resolve_team_player_ids

    teams = session.query(EventTeam).filter(EventTeam.event_id == event.id).all()
    rosters: dict = {}
    all_ids: set = set()
    for team in teams:
        ids = resolve_team_player_ids(session, team)
        rosters[team.id] = ids
        all_ids.update(ids)
    names: dict = {}
    if all_ids:
        names = {int(pid): nm for pid, nm in
                 session.query(Player.player_id, Player.player_name)
                 .filter(Player.player_id.in_(sorted(all_ids))).all()}
    return {tid: sorted(((pid, names.get(pid) or f"Player {pid}") for pid in ids),
                        key=lambda r: r[1].lower())
            for tid, ids in rosters.items()}


def payout_plan(session, event) -> dict:
    """The admin payout view for one event. Read-only."""
    from db.models import EventBuyin, EventTeam
    from services import event_point_awards as epa
    from services.event_prizes import effective_prize_config, pot_summary

    team_count = (session.query(EventTeam)
                  .filter(EventTeam.event_id == event.id).count())
    cfg = effective_prize_config(getattr(event, "prize_config", None),
                                 team_count=team_count)
    pot = pot_summary(session, event, team_count=team_count)
    status = getattr(event, "status", None) or "active"
    base = {
        "enabled": bool(pot["enabled"]),
        "final": status == "past",
        "status": status,
        "distribution": cfg["distribution"],
        "top_n": cfg["top_n"],
        "splits": cfg["splits"],
        "active_only": bool(cfg.get("payout_active_only")),
        "total": int(pot["total"]),
    }
    if not pot["enabled"]:
        return {**base, "winners": [], "unclaimed": 0, "unclaimed_places": [],
                "rounding": 0, "unallocated": 0, "individual": False}

    competition_kind = epa._is_competition(event)
    individual = competition_kind and not epa._is_team_race(session, event)
    kind = getattr(event, "kind", None) or "standard"

    rosters = _team_rosters(session, event)
    if individual:
        ranked, mode = epa._competition_standings(session, event)
        placements = epa._competition_placements(ranked, mode)
        names = {pid: nm for members in rosters.values() for pid, nm in members}
        for r in ranked:
            if r.get("player_id") is not None and r.get("player_name"):
                names.setdefault(int(r["player_id"]), r["player_name"])
        labels = {pid: names.get(pid) or f"Player {pid}" for pid in placements}
        members_of = {pid: [(pid, labels[pid])] for pid in placements}
    else:
        if kind == "conquest":
            placements, labels = _conquest_placements(session, event)
        else:
            placements, labels = epa._team_placements(session, event)
        members_of = {tid: rosters.get(tid, []) for tid in placements}

    shares = place_shares(cfg["distribution"], cfg["top_n"], cfg["splits"])
    alloc = allocate(pot["total"], shares, placements.items())

    all_member_ids = sorted({pid for ms in members_of.values() for pid, _ in ms})
    took_part = epa._took_part(session, event.id, all_member_ids)
    if individual:
        took_part |= set(placements)
    paid_buyins = {
        int(pid) for (pid,) in
        session.query(EventBuyin.player_id)
        .filter(EventBuyin.event_id == event.id, EventBuyin.kind == "buyin",
                EventBuyin.status == "paid", EventBuyin.player_id.isnot(None))
        .distinct().all()
    }

    winners = []
    unallocated = 0
    for entity, info in sorted(alloc["entities"].items(),
                               key=lambda kv: (kv[1]["place"], str(labels.get(kv[0])))):
        if info["share"] == 0:
            continue  # placed, but outside the paid places
        roster = members_of.get(entity, [])
        eligible = [pid for pid, _ in roster
                    if not base["active_only"] or pid in took_part]
        split, remainder = split_evenly(info["amount"], eligible)
        if not eligible:
            unallocated += info["amount"]
        winners.append({
            "kind": "player" if individual else "team",
            "id": int(entity),
            "name": labels.get(entity) or str(entity),
            "place": info["place"],
            "tied": info["tied"],
            "share_pct": share_percent(info["share"]),
            "amount": info["amount"],
            "member_remainder": remainder if eligible else 0,
            "members": [{
                "player_id": int(pid),
                "player_name": nm,
                "amount": int(split.get(pid, 0)),
                "eligible": pid in split,
                "took_part": pid in took_part,
                "paid_buyin": pid in paid_buyins,
            } for pid, nm in roster],
        })
    return {
        **base,
        "individual": individual,
        "winners": winners,
        "unclaimed": alloc["unclaimed"],
        "unclaimed_places": alloc["unclaimed_places"],
        "rounding": alloc["rounding"] + sum(w["member_remainder"] for w in winners),
        "unallocated": unallocated,
    }
