"""Derive kills-per-hour rates for bosses WOM publishes no EHB rate for.

WOM's ``/efficiency/rates?metric=ehb`` table covers ~66 bosses. Anything
absent — which is exactly the newer content clan bingos concentrate on —
contributes real activity but 0 EHB (``services/event_effort.ehb_hours``
returns the honest zero rather than guessing). Measured on the 2026-07-28
backfill, 69% of all effort kills were at unpriced bosses (Maggot King,
Zalcano).

This script fills ``npc_ehb_rates`` from DropTracker's own data, two methods
in fidelity order:

``pb_median``
    3,600,000 / median ``personal_best.kill_time`` (ms). One row per player
    per boss, and ``kill_time`` is the *latest submitted* kill (not the PB),
    so the median is "a typical player's recent kill". Calibrated 2026-07-28
    against WOM's published rates on priced bosses — factor ≈ 1.0, no
    haircut needed (zulrah 42.4 vs 46, vorkath 33.0 vs 34, araxxor 40.9 vs
    40, grotesque 36.8 vs 37):
    WOM's "efficient" rates match in-fight kill cadence.

``drop_gaps``
    For bosses the plugin doesn't time (Zalcano). Per player: collapse drops
    within ``BURST_SECONDS`` into one kill event (a kill's multi-item loot
    arrives as one burst — this is what keeps loot-events from counting as
    kills), take the median gap between events (gaps bounded to
    [MIN_GAP, MAX_GAP] so logouts and breaks fall out), giving that player's
    real kills/hour. The published rate is the p90 across players: WOM rates
    assume *efficient* play, and on priced bosses p90 lands at 0.83–0.92 of
    WOM where the per-player median reads ~35% low. Candidate players come
    from ``player_npc_hourly_totals`` and drops are fetched per player via
    the (player_id, date_added) index — never a full-table scan.

``partial_gaps`` (``--partials``)
    For ``services/event_effort.COMPLETION_MARKERS`` NPCs, where the content
    pays out for a partial attempt so the plugin's KC and WOM's metric count
    different things. The published rate is the p90 of per-player median gaps
    between consecutive attempts that did NOT carry the completion marker
    item — i.e. how fast a *bail* actually goes.

    Measured for the Colosseum on 2026-08-28: 4,516 attempts, 14% completions;
    a bail's median inter-attempt gap is 1.9 min against 34.6 min for a
    completion. Charging bails the completion rate is what put one player at
    329 EHE hours.

Guard rails (suggestions thread #93):

* A derived rate is only ever a FALLBACK — the read path checks WOM first, so
  nothing here can override a published rate. Priced bosses are skipped by
  default (``--include-priced`` computes them for calibration display only,
  and never writes them). ``partial_gaps`` is the one method that writes for a
  WOM-priced boss, and it still overrides nothing: the WOM rate keeps pricing
  completions, this rate prices only the attempts WOM never counted.
* Below the sample thresholds no row is written — the boss keeps its honest
  0 EHB rather than a rate invented from noise.
* Clue caskets / clue-scroll pseudo-NPCs are excluded outright: stacked
  caskets opened in bulk would produce an absurd rate, and clue sources are
  already excluded from effort scoring.

Candidates are every NPC referenced by ``web_event_effort`` (current need)
plus every NPC with ``personal_best`` timings (future need, cheap), so rates
usually exist before an event ever asks for them. Re-running refreshes
``rate_kph``/``computed_at`` in place — idempotent.

Usage
-----
    python -m scripts.compute_npc_ehb_rates                    # dry run
    python -m scripts.compute_npc_ehb_rates --apply
    python -m scripts.compute_npc_ehb_rates --npc 15742 --apply
    python -m scripts.compute_npc_ehb_rates --include-priced   # calibration
    python -m scripts.compute_npc_ehb_rates --partials --apply # marker NPCs
"""

import argparse
import statistics
import sys
from datetime import datetime

sys.path.insert(0, ".")

# ── pb_median ────────────────────────────────────────────────────────────────
#: Fewest personal_best rows (≈ players) to trust a median kill time.
PB_MIN_SAMPLES = 30
#: Sanity bounds on a single kill duration (ms): sub-10s rows are lag/junk,
#: 2h+ rows are stuck timers.
PB_MIN_TIME_MS = 10_000
PB_MAX_TIME_MS = 2 * 3600 * 1000

# ── drop_gaps ────────────────────────────────────────────────────────────────
#: Drops closer together than this are one kill's loot burst, not two kills.
BURST_SECONDS = 10
#: Gap bounds: below MIN is same-kill spillover, above MAX is a break/logout.
MIN_GAP_SECONDS = 15
MAX_GAP_SECONDS = 1800
#: A player needs this many qualifying gaps for their personal rate to count.
MIN_GAPS_PER_PLAYER = 5
#: Fewest qualifying players to trust the cross-player percentile.
GAPS_MIN_PLAYERS = 15
#: WOM rates assume efficient play; p90 of per-player rates calibrates to
#: 0.83–0.92 of WOM on priced bosses (the median reads ~35% low).
GAPS_PERCENTILE = 90
#: Window and per-NPC player cap — keeps the drops reads bounded.
GAPS_WINDOW_DAYS = 30
GAPS_MAX_PLAYERS = 200

#: Never derive a rate for clue pseudo-NPCs: stacked caskets opened in bulk
#: give a meaningless cadence, and clue sources are excluded from effort.
CLUE_NAME_TOKENS = ("clue scroll", "casket")

#: Sanity bounds on any published rate (kills/hour). Nothing in OSRS is
#: legitimately killed 4-figure times an hour; sub-1 rates price a single
#: kill at over an hour of EHB, which overstates more than it informs.
RATE_MIN_KPH = 1.0
RATE_MAX_KPH = 400.0


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = (len(sorted_vals) - 1) * p / 100.0
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def _candidates(session, only_npc):
    """``{npc_id: (name, metric)}`` for every NPC that might need a rate."""
    from sqlalchemy import text
    from utils.wiseoldman import wom_boss_metric

    if only_npc:
        rows = session.execute(
            text("SELECT npc_id, npc_name FROM npc_list WHERE npc_id IN :ids")
            .bindparams(ids=tuple(only_npc)) if len(only_npc) > 1 else
            text("SELECT npc_id, npc_name FROM npc_list WHERE npc_id = :id")
            .bindparams(id=only_npc[0])).fetchall()
    else:
        rows = session.execute(text("""
            SELECT n.npc_id, n.npc_name FROM npc_list n
            WHERE n.npc_id IN (SELECT DISTINCT npc_id FROM web_event_effort)
               OR n.npc_id IN (SELECT DISTINCT npc_id FROM personal_best
                               WHERE npc_id IS NOT NULL)
        """)).fetchall()
    out = {}
    for npc_id, name in rows:
        lowered = (name or "").lower()
        if any(tok in lowered for tok in CLUE_NAME_TOKENS):
            continue
        out[int(npc_id)] = (name, wom_boss_metric(name))
    return out


def _pb_median_rate(session, npc_id):
    """(rate_kph, sample_size) from personal_best timings, or None."""
    from sqlalchemy import text

    times = [t for (t,) in session.execute(text("""
        SELECT kill_time FROM personal_best
        WHERE npc_id = :n AND kill_time BETWEEN :lo AND :hi
    """), {"n": npc_id, "lo": PB_MIN_TIME_MS, "hi": PB_MAX_TIME_MS})]
    if len(times) < PB_MIN_SAMPLES:
        return None
    return 3_600_000.0 / statistics.median(times), len(times)


def _drop_gaps_rate(session, npc_id):
    """(rate_kph, qualifying_players) from inter-drop-burst gaps, or None."""
    from sqlalchemy import text

    since_hour = session.execute(text(
        "SELECT DATE_FORMAT(NOW() - INTERVAL :d DAY, '%Y-%m-%d-%H')"),
        {"d": GAPS_WINDOW_DAYS}).scalar()
    players = [p for (p,) in session.execute(text("""
        SELECT player_id FROM player_npc_hourly_totals
        WHERE npc_id = :n AND date_hour >= :h
        GROUP BY player_id ORDER BY SUM(drop_count) DESC LIMIT :lim
    """), {"n": npc_id, "h": since_hour, "lim": GAPS_MAX_PLAYERS})]
    if len(players) < GAPS_MIN_PLAYERS:
        return None

    per_player = []
    for pid in players:
        # (player_id, date_added) composite index; npc filtered server-side.
        ts = sorted(t for (t,) in session.execute(text("""
            SELECT date_added FROM drops
            WHERE player_id = :p AND npc_id = :n
              AND date_added >= NOW() - INTERVAL :d DAY
        """), {"p": pid, "n": npc_id, "d": GAPS_WINDOW_DAYS}))
        events, last = [], None
        for t in ts:
            if last is None or (t - last).total_seconds() > BURST_SECONDS:
                events.append(t)
            last = t
        gaps = [
            (b - a).total_seconds() for a, b in zip(events, events[1:])
            if MIN_GAP_SECONDS <= (b - a).total_seconds() <= MAX_GAP_SECONDS
        ]
        if len(gaps) >= MIN_GAPS_PER_PLAYER:
            per_player.append(3600.0 / statistics.median(gaps))
    if len(per_player) < GAPS_MIN_PLAYERS:
        return None
    return _pct(sorted(per_player), GAPS_PERCENTILE), len(per_player)


def _partial_gaps_rate(session, npc_id, marker_item: str):
    """(rate_kph, qualifying_players) for a COMPLETION_MARKERS NPC's PARTIAL
    attempts, or None.

    An attempt is one (player, kill_count) pair; it completed if any of its
    drops was ``marker_item``. The gap from the previous attempt is that
    attempt's duration plus re-entry, so gaps are read only between
    CONSECUTIVE kill counts — a jump means an unreported attempt sat in
    between and the gap would cover both.

    Same shape as :func:`_drop_gaps_rate` (per-player median, p90 across
    players, identical bounds) so a partial rate is comparable to every other
    derived rate rather than a second convention.
    """
    from sqlalchemy import text

    rows = session.execute(text("""
        SELECT d.player_id, d.kill_count, MIN(d.date_added) AS at_,
               MAX(i.item_name = :item) AS completed
        FROM drops d JOIN items i ON i.item_id = d.item_id
        WHERE d.npc_id = :n AND d.kill_count IS NOT NULL
          AND d.date_added >= NOW() - INTERVAL :d DAY
        GROUP BY d.player_id, d.kill_count
        ORDER BY d.player_id, d.kill_count
    """), {"n": npc_id, "item": marker_item, "d": GAPS_WINDOW_DAYS}).fetchall()

    per_player, gaps, prev = [], [], None
    for player_id, kc, at_, completed in rows:
        if (prev is not None and prev[0] == player_id and int(kc) == prev[1] + 1
                and not int(completed or 0)):
            gap = (at_ - prev[2]).total_seconds()
            if MIN_GAP_SECONDS <= gap <= MAX_GAP_SECONDS:
                gaps.append(gap)
        elif prev is not None and prev[0] != player_id:
            if len(gaps) >= MIN_GAPS_PER_PLAYER:
                per_player.append(3600.0 / statistics.median(gaps))
            gaps = []
        prev = (player_id, int(kc), at_)
    if len(gaps) >= MIN_GAPS_PER_PLAYER:
        per_player.append(3600.0 / statistics.median(gaps))

    if len(per_player) < GAPS_MIN_PLAYERS:
        return None
    return _pct(sorted(per_player), GAPS_PERCENTILE), len(per_player)


def partial_rate_is_sane(rate, wom_rate) -> bool:
    """Whether a measured partial-attempt rate is publishable.

    A partial attempt is a fraction of a full run, so it can only be FASTER
    than a completion — strictly more of them per hour. Measuring otherwise
    means the marker item stopped identifying completions (an item rename, a
    drop-table change, a plugin that stopped sending it) and every attempt is
    being read as a bail. Refusing to publish keeps the last good rate instead
    of quietly redefining what the number means.

    With no WOM rate to compare against there is nothing to check, so this
    passes — the caller warns separately that completions will price at 0.
    """
    try:
        rate = float(rate)
        wom_rate = float(wom_rate or 0)
    except (TypeError, ValueError):
        return False
    if rate <= 0:
        return False
    return wom_rate <= 0 or rate > wom_rate


def _marker_npc_ids(session) -> set:
    """npc_ids of every ``COMPLETION_MARKERS`` NPC — the rows the normal pass
    must never touch, because their rate measures a different quantity."""
    from sqlalchemy import text
    from services.event_effort import COMPLETION_MARKERS

    ids = set()
    for npc_norm in COMPLETION_MARKERS:
        row = session.execute(text(
            "SELECT npc_id FROM npc_list WHERE LOWER(npc_name) = :n"),
            {"n": npc_norm}).fetchone()
        if row is not None:
            ids.add(int(row[0]))
    return ids


def _run_partials(session, apply: bool, wom_rates: dict) -> int:
    """Compute + write the partial-attempt rate for every marker NPC."""
    from sqlalchemy import text
    from db.models import NpcEhbRate
    from services.event_effort import COMPLETION_MARKERS

    written = 0
    print(f"{'npc':>7}  {'name':32} {'method':12} {'kph':>7} {'n':>5}  note")
    for npc_norm, marker in sorted(COMPLETION_MARKERS.items()):
        row = session.execute(text(
            "SELECT npc_id, npc_name FROM npc_list WHERE LOWER(npc_name) = :n"),
            {"n": npc_norm}).fetchone()
        if row is None:
            print(f"{'—':>7}  {npc_norm:32.32} {'—':12} {'—':>7} {'—':>5}  "
                  f"no npc_list row, skipped")
            continue
        npc_id, name = int(row[0]), row[1]
        result = _partial_gaps_rate(session, npc_id, marker["item"])
        if result is None:
            print(f"{npc_id:>7}  {(name or ''):32.32} {'partial_gaps':12} "
                  f"{'—':>7} {'—':>5}  too few samples, keeping 0 EHB")
            continue
        rate, n = result
        if not (RATE_MIN_KPH <= rate <= RATE_MAX_KPH):
            print(f"{npc_id:>7}  {(name or ''):32.32} {'partial_gaps':12} "
                  f"{rate:>7.1f} {n:>5}  outside sanity bounds, skipped")
            continue
        wom = wom_rates.get(marker["metric"])
        if not partial_rate_is_sane(rate, wom):
            print(f"{npc_id:>7}  {(name or ''):32.32} {'partial_gaps':12} "
                  f"{rate:>7.1f} {n:>5}  NOT FASTER than the completion rate "
                  f"({wom}/h) — marker item may be stale, keeping previous")
            continue
        if not wom:
            print(f"{npc_id:>7}  {(name or ''):32.32} {'partial_gaps':12} "
                  f"{rate:>7.1f} {n:>5}  WARNING: WOM publishes no "
                  f"{marker['metric']} rate — completions will price at 0")
        print(f"{npc_id:>7}  {(name or ''):32.32} {'partial_gaps':12} "
              f"{rate:>7.1f} {n:>5}  partial attempts only "
              f"(completions keep {marker['metric']})")
        if apply:
            existing = session.get(NpcEhbRate, npc_id)
            if existing is None:
                session.add(NpcEhbRate(
                    npc_id=npc_id, boss_metric=marker["metric"],
                    rate_kph=round(rate, 2), sample_size=n,
                    method="partial_gaps", computed_at=datetime.now()))
            else:
                existing.rate_kph = round(rate, 2)
                existing.sample_size = n
                existing.method = "partial_gaps"
                existing.computed_at = datetime.now()
            written += 1
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npc", type=int, action="append",
                        help="restrict to specific npc_id(s); repeatable")
    parser.add_argument("--include-priced", action="store_true",
                        help="also compute WOM-priced bosses for calibration "
                             "display (their rows are NEVER written)")
    parser.add_argument("--apply", action="store_true",
                        help="write the rates (default: dry run)")
    parser.add_argument("--partials", action="store_true",
                        help="run ONLY the partial-attempt pass for "
                             "COMPLETION_MARKERS NPCs (it is part of a normal "
                             "run too, so the weekly timer recalibrates it)")
    args = parser.parse_args()

    from db import Session
    from db.models import NpcEhbRate
    from utils.wiseoldman import get_ehb_rates_sync

    wom_rates = get_ehb_rates_sync() or {}
    if not wom_rates:
        # The WOM table is the guard that stops us shadowing a published rate;
        # without it we cannot know which bosses actually need one.
        sys.exit("WOM EHB rate cache is empty — refusing to run "
                 "(the events worker keeps it warm; is Redis up?)")
    print(f"WOM publishes rates for {len(wom_rates)} bosses")

    session = Session()
    written = 0
    if args.partials:
        try:
            written = _run_partials(session, args.apply, wom_rates)
            if args.apply and written:
                session.commit()
                print(f"\nwrote {written} partial rate(s)")
            else:
                session.rollback()
                print(f"\n{written or 0} partial rate(s) would be written "
                      "— re-run with --apply")
        finally:
            session.close()
        return 0
    try:
        candidates = _candidates(session, args.npc)
        print(f"{len(candidates)} candidate NPC(s)\n")
        print(f"{'npc':>7}  {'name':32} {'metric':28} {'method':10} "
              f"{'kph':>7} {'n':>5}  note")
        marker_ids = _marker_npc_ids(session)
        for npc_id in sorted(candidates):
            name, metric = candidates[npc_id]
            if npc_id in marker_ids:
                # A marker NPC's npc_ehb_rates row means "partial attempts per
                # hour", not "kills per hour" — a different quantity that only
                # --partials knows how to measure. Today WOM prices
                # sol_heredit, so the `wom` guard below would skip it anyway;
                # this does not rely on that. If WOM ever renames or drops the
                # metric, the incidental guard evaporates and this pass would
                # overwrite the partial rate with an all-attempts cadence,
                # silently changing what the number means and putting bails
                # back on the completion clock.
                print(f"{npc_id:>7}  {(name or ''):32.32} {(metric or '—'):28} "
                      f"{'—':10} {'—':>7} {'—':>5}  completion-marker NPC, "
                      f"only --partials may write it")
                continue
            wom = wom_rates.get(metric) if metric else None
            if wom and not args.include_priced:
                continue

            result, method = _pb_median_rate(session, npc_id), "pb_median"
            if result is None:
                result, method = _drop_gaps_rate(session, npc_id), "drop_gaps"
            if result is None or not result[0]:
                print(f"{npc_id:>7}  {(name or ''):32.32} {(metric or '—'):28} "
                      f"{'—':10} {'—':>7} {'—':>5}  too few samples, keeping 0 EHB")
                continue
            rate, n = result
            if not (RATE_MIN_KPH <= rate <= RATE_MAX_KPH):
                print(f"{npc_id:>7}  {(name or ''):32.32} {(metric or '—'):28} "
                      f"{method:10} {rate:>7.1f} {n:>5}  outside sanity bounds, skipped")
                continue

            note = ""
            if wom:
                note = (f"WOM publishes {wom} (calibration only, NOT written; "
                        f"factor {wom / rate:.2f})")
            print(f"{npc_id:>7}  {(name or ''):32.32} {(metric or '—'):28} "
                  f"{method:10} {rate:>7.1f} {n:>5}  {note}")
            if wom or not args.apply:
                written += 0 if wom else 1
                continue

            row = session.get(NpcEhbRate, npc_id)
            if row is None:
                session.add(NpcEhbRate(
                    npc_id=npc_id, boss_metric=metric, rate_kph=round(rate, 2),
                    sample_size=n, method=method, computed_at=datetime.now()))
            else:
                row.boss_metric = metric
                row.rate_kph = round(rate, 2)
                row.sample_size = n
                row.method = method
                row.computed_at = datetime.now()
            written += 1

        # Marker NPCs are excluded from the pass above, so their rate has to be
        # recomputed here or it never recalibrates at all — it is measured from
        # a rolling 30-day window like every other derived rate, and a rate
        # that silently freezes at whatever the content looked like on the day
        # it shipped is exactly the kind of slow drift nobody notices.
        print()
        partials = _run_partials(session, args.apply, wom_rates)

        if args.apply:
            session.commit()
            print(f"\napplied: {written} rate(s) + {partials} partial rate(s) "
                  f"written to npc_ehb_rates")
        else:
            print(f"\ndry run: {written} rate(s) + {partials} partial rate(s) "
                  f"would be written (re-run with --apply)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
