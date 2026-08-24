"""Canonical "monthly loot total" for a group's Discord surfaces.

``lootboard/generator.py`` owns the arithmetic behind the number a group sees on
its board: the WOM roster, minus the group's ``ignored_players``, minus the
drops this group excluded through ``manual_submission_policy``, plus/minus the
split-GP deltas from ``services/split_credits.py``. Every render publishes the
result to ``gleaderboard:{partition}``, which the website's groups tab
(``api/routes/groups.py``) and the monthly recap (``services/recap.py``) already
read instead of recomputing.

The voice-channel counter (``services/channel_names.py``) was the one surface
that derived the total on its own — a plain sum of each WOM member's *global*
``player:{id}:{partition}:total_loot`` — so it silently skipped every one of
those adjustments. A Pegasus PvM (group 14) leader hid three members on
2026-08-14/17/18; the board dropped their loot, the voice channel kept counting
it, and the two displays sat exactly 984,615,122 gp apart with the gap widening
daily (reported as suggestion #138).

So: read the published total, never re-derive it. One implementation of the
arithmetic means the board and the voice channel cannot drift apart again when
the board's rules change.
"""

from typing import Optional


def group_totals_key(partition) -> str:
    """Sorted set of per-group board totals for one monthly partition.

    Member is the ``group_id``, score is the total the board drew. Written by
    ``lootboard/generator.py`` on every render; mirrored by
    ``web_api.common.group_totals_key``.
    """
    return f"gleaderboard:{partition}"


def board_month_total(group_id, partition, *, redis_conn=None) -> Optional[int]:
    """The loot total the lootboard last drew for ``group_id``.

    Returns ``None`` when no board has been rendered for that partition yet —
    a brand-new group, or the first minutes after a month rollover — and when
    Redis is unreachable. Callers should display nothing in that case rather
    than substituting a figure of their own: an independently computed number
    is exactly the drift this module exists to prevent.
    """
    if redis_conn is None:
        from utils.redis import redis_client

        redis_conn = getattr(redis_client, "client", None)
    if redis_conn is None:
        return None
    try:
        score = redis_conn.zscore(group_totals_key(partition), int(group_id))
    except Exception as e:
        print(f"board_month_total: couldn't read {group_totals_key(partition)}: {e}")
        return None
    if score is None:
        return None
    try:
        return int(float(score))
    except (TypeError, ValueError):
        return None
