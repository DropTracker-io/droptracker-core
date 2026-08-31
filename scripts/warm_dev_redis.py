#!/usr/bin/env python3
"""Warm this dev box's Redis from the loaded drops.

The monthly boards (leaderboard:{YYYYMM}) and the per-player monthly totals
(player:{id}:{YYYYMM}:total_loot) are what the site and API actually read.
reconcile_period_leaderboards.py only rebuilds the daily/weekly boards for one
ISO week, and reconcile_all_time_leaderboards.py only does leaderboard:all --
neither populates the monthly keys, so a freshly loaded dev DB renders
"No ranked players yet" until this runs.
"""
import sys
import time

sys.path.insert(0, "/store/droptracker/disc")

from services.redis_updates import (  # noqa: E402
    force_update_all_current_month_players,
    get_players_with_current_month_drops,
)

start = time.time()
players = get_players_with_current_month_drops()
print("players with current-month drops: %d" % len(players))

seen = {"n": 0}


def progress(*args):
    seen["n"] += 1
    if seen["n"] % 250 == 0:
        print("  ...%d processed (%.0fs)" % (seen["n"], time.time() - start), flush=True)


result = force_update_all_current_month_players(progress_callback=progress)
print("result: %s" % (result,))
print("elapsed: %.0fs" % (time.time() - start))
