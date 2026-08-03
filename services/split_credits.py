"""Canonical split-GP arithmetic for a group's leaderboard.

`split_gp_tracking` redistributes **group** leaderboard credit: a drop's
receiver keeps only their share and each in-group participant is credited one
share (see ``data/submissions/drop.py::_award_split_gp_credits``). Redis holds
that as incremental adjustments on ``leaderboard:{partition}:group:{gid}``,
which is what the website renders.

The Discord lootboard computes its leaderboard panel independently, from each
player's *global* ``player:{id}:{partition}:total_loot``, so it never saw any of
those adjustments — a split receiver showed the drop's full value on the board
while the website showed their share, and the participants' credits were missing
entirely. This module is the one place that turns ``drop_splits`` rows into
per-player deltas, so the board and the site can't drift apart again.

Deltas are relative to a player's raw loot total:
  * participant → ``+split_value`` for each drop they were cut in on;
  * receiver    → ``-(drop_value - split_value)``, applied once per drop
    regardless of how many participants it had.

Shares belonging to people the group can't credit (untracked, not in the group,
or left unnamed) are intentionally not redistributed — they come off the
receiver and are credited to nobody, so a group's board total reflects only what
the group actually kept.
"""
from __future__ import annotations

from collections import defaultdict


def group_split_deltas(session, group_id: int, player_ids, partition) -> dict:
    """Per-player GP deltas from ``drop_splits`` for one group + partition.

    Returns ``{player_id: delta}`` covering only players in ``player_ids``
    (the board's member list) with a non-zero adjustment. Hidden drops are
    skipped — they are removed from the board's totals already, so applying
    their split would double-subtract.
    """
    from db.models import Drop
    from db.models.drop_split import DropSplit

    try:
        partition_int = int(str(partition).replace("-", ""))
    except (TypeError, ValueError):
        return {}

    # The board's member list is assembled from WOM ids and can carry Nones for
    # unresolved accounts (group 296 has a NULL player_id association row) —
    # skip them rather than letting int() take the whole adjustment down.
    members = set()
    for p in player_ids or []:
        try:
            members.add(int(p))
        except (TypeError, ValueError):
            continue
    if not members:
        return {}

    rows = (
        session.query(DropSplit, Drop)
        .join(Drop, Drop.drop_id == DropSplit.drop_id)
        .filter(
            DropSplit.group_id == group_id,
            Drop.partition == partition_int,
            Drop.hidden != True,  # noqa: E712
        )
        .all()
    )
    if not rows:
        return {}

    deltas = defaultdict(int)
    # The receiver is adjusted once per drop, not once per participant row.
    receiver_seen = set()
    for split_row, drop in rows:
        # Also filtered in SQL above; re-checked here so the invariant holds
        # no matter how the rows were fetched. Double-subtracting a hidden
        # drop's split would silently understate the whole group's board.
        if drop.hidden:
            continue
        if split_row.player_id in members:
            deltas[split_row.player_id] += int(split_row.split_value)

        if drop.drop_id in receiver_seen:
            continue
        receiver_seen.add(drop.drop_id)
        if drop.player_id not in members:
            continue
        drop_value = int(drop.value) * int(drop.quantity)
        reduction = drop_value - int(split_row.split_value)
        if reduction > 0:
            deltas[drop.player_id] -= reduction

    return {pid: d for pid, d in deltas.items() if d}


def group_has_split_tracking(session, group_id: int) -> bool:
    """Whether `group_id` has split GP tracking switched on."""
    from db.models import GroupConfiguration

    return (
        session.query(GroupConfiguration.group_id)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "split_gp_tracking",
            GroupConfiguration.config_value == "1",
        )
        .first()
        is not None
    )
