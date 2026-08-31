"""KC milestone detection — "1st kill, then every Nth" group announcements.

Fed from the two submission types that carry an absolute kill count: drops
(``kill_count``, web76a) and personal-best/timed-kill messages (``killcount``,
which the plugin sends on EVERY timed boss kill, not just PBs). The stored side
of the comparison is ``player_npc_kc`` (web108a), a per-(player, npc) watermark
advanced in the same transaction as the notification enqueue.

The milestone test is a CROSSING test — ``prev < m <= new`` — never membership:
a membership test re-announces the milestone on every later submission carrying
the same KC (the total-level-milestone lesson, experience.py). Only the highest
crossed milestone announces per submission, so a group that configures interval
100 and then sees a 99→400 jump gets one message about 400, not three.

Seeding, precisely (``prev`` = stored watermark, ``new`` = submitted KC):

* no row, ``new == 1``  → record + announceable first kill;
* no row, ``new > 1``   → seed silently (500 KC of pre-install history is
  stale news, and announcing every crossed milestone would flood the channel);
* ``prev >= new``       → no-op (out-of-order delivery, duplicate, or a
  counter regression);
* ``new - prev > KC_SEED_GAP_BOUND`` → silent re-seed. Two *consecutive* plugin
  reports hundreds of kills apart mean a divergent counter (the plugin's chest
  count vs another source — the Fortis Colosseum trap), a relink, or a long
  tracking outage — all likelier than a legitimate grind we should announce.

Scope gates (checked before any DB write): main world only (drops.kill_count
itself is main-world only), plugin-path submissions only (manual submissions
are untrusted here), and WOM-recognized bosses only — ``wom_boss_metric``
resolving is the codebase's cleanest "is actually a boss" test, and it keeps
"1st kill of a Goblin" out of everyone's Discord.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import IntegrityError

from .common import (
    create_notification,
    debug_print,
    get_player_groups_with_global,
    is_truthy_config,
)

#: A consecutive-report KC jump bigger than this re-seeds instead of
#: announcing. Aligned with the event engine's _KC_DELTA_ALERT.
KC_SEED_GAP_BOUND = 250

#: Group config keys (mirrored in web_api/config_registry.py and the TS
#: registry). notify_kc_milestones is the family's master toggle.
CONFIG_MASTER_KEY = "notify_kc_milestones"
CONFIG_FIRST_KILL_KEY = "notify_first_kc"
CONFIG_INTERVAL_KEY = "kc_milestone_interval"
DEFAULT_INTERVAL = 100


def parse_kill_count(raw) -> Optional[int]:
    """A usable absolute KC, or None.

    Same rules as the drop processor: the plugin sends 0 to mean "unknown",
    so 0 and negatives are None, as is anything non-numeric.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def highest_crossed_milestone(prev: int, new: int, interval) -> Optional[int]:
    """The highest multiple of ``interval`` in ``(prev, new]``, or None.

    A crossing test against the stored watermark, so a milestone announces
    exactly once no matter how many submissions repeat the same KC.
    """
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        return None
    if interval <= 0 or new <= prev:
        return None
    highest = (new // interval) * interval
    return highest if highest > prev else None


def should_seed_silently(prev: Optional[int], new: int, bound: int = KC_SEED_GAP_BOUND) -> bool:
    """Whether this KC establishes/re-establishes a baseline with no announcement."""
    if prev is None:
        return new > 1
    return (new - prev) > bound


@dataclass
class KcResult:
    previous: Optional[int]  # stored watermark before this submission (None = no row)
    current: int             # the submitted KC now stored
    is_first_kill: bool      # announceable "1st kill" (no row existed, new == 1)
    seeded: bool             # baseline (re-)established silently
    advanced: bool           # the watermark moved (False = no-op duplicate/regression)


async def record_kill_count(session, player_id: int, npc_id: int, new_kc: int) -> KcResult:
    """Advance the (player, npc) watermark and classify what happened.

    Row-locks the watermark (SELECT ... FOR UPDATE) so the 6-worker consumer
    serializes concurrent submissions for the same pair: the loser of a
    same-kill race re-reads the winner's KC and classifies as a no-op. The
    insert race goes through a SAVEPOINT (the pb.py idiom) — an IntegrityError
    means another worker created the row first, so re-read it locked and take
    the update path.
    """
    from db.models import PlayerNpcKc

    row = (
        session.query(PlayerNpcKc)
        .filter(PlayerNpcKc.player_id == player_id, PlayerNpcKc.npc_id == npc_id)
        .with_for_update()
        .first()
    )
    if row is None:
        try:
            with session.begin_nested():
                session.add(PlayerNpcKc(player_id=player_id, npc_id=npc_id, kill_count=new_kc))
                session.flush()
        except IntegrityError:
            row = (
                session.query(PlayerNpcKc)
                .filter(PlayerNpcKc.player_id == player_id, PlayerNpcKc.npc_id == npc_id)
                .with_for_update()
                .first()
            )
            if row is None:
                # The competing insert rolled back between raising our
                # IntegrityError and our re-read. Vanishingly rare; skip this
                # check rather than retry-loop inside the caller's transaction.
                return KcResult(previous=None, current=new_kc, is_first_kill=False,
                                seeded=False, advanced=False)
        else:
            return KcResult(
                previous=None,
                current=new_kc,
                is_first_kill=new_kc == 1,
                seeded=should_seed_silently(None, new_kc),
                advanced=True,
            )

    prev = int(row.kill_count)
    if prev >= new_kc:
        return KcResult(previous=prev, current=prev, is_first_kill=False, seeded=False, advanced=False)

    row.kill_count = new_kc
    return KcResult(
        previous=prev,
        current=new_kc,
        is_first_kill=False,
        seeded=should_seed_silently(prev, new_kc),
        advanced=True,
    )


async def handle_kill_count(
    session,
    player,
    npc_id: int,
    npc_name: str,
    raw_kill_count,
    *,
    world_type: str = "main",
    from_plugin: bool = True,
    image_url: str = "",
    plugin_version=None,
    use_external_session: bool = False,
    player_groups=None,
) -> None:
    """Record a reported KC and announce any milestone it crossed.

    The single entry point for both feeder processors (drop + pb). Never
    raises past itself by contract with its callers — but DB writes here are
    part of the caller's transaction, so a failure inside record_kill_count
    is allowed to propagate (the callers wrap this in their own try/except
    "never fail the submission" guard).
    """
    if world_type != "main" or not from_plugin:
        return
    kill_count = parse_kill_count(raw_kill_count)
    if kill_count is None or player is None or not npc_id:
        return

    # WOM-recognized bosses only. Lazy import: wiseoldman pulls in the wom
    # client library, which the unit-test bootstrap stubs.
    from utils.wiseoldman import wom_boss_metric

    if not wom_boss_metric(npc_name):
        return

    result = await record_kill_count(session, player.player_id, npc_id, kill_count)
    if not result.advanced or result.seeded:
        if result.seeded:
            debug_print(
                f"KC watermark seeded silently for player {player.player_id} "
                f"npc {npc_id} ({npc_name}): {result.previous} -> {result.current}"
            )
        return

    if player_groups is None:
        player_groups = get_player_groups_with_global(session, player)

    player_id = player.player_id
    player_name = player.player_name
    external = session if use_external_session else None

    from utils import group_config as gc

    for group in player_groups:
        await asyncio.sleep(0)  # yield to the event loop, like the other group loops
        group_id = group.group_id

        if not is_truthy_config(gc.get(session, group_id, CONFIG_MASTER_KEY, "0")):
            continue

        milestone = None
        if not result.is_first_kill:
            interval = gc.get(session, group_id, CONFIG_INTERVAL_KEY, DEFAULT_INTERVAL)
            milestone = highest_crossed_milestone(
                result.previous if result.previous is not None else 0,
                result.current,
                interval if interval is not None else DEFAULT_INTERVAL,
            )
            if milestone is None:
                continue
        else:
            first_kill_setting = gc.get(session, group_id, CONFIG_FIRST_KILL_KEY, "1")
            if not is_truthy_config(first_kill_setting):
                continue

        await create_notification(
            "kc_milestone",
            player_id,
            {
                "player_name": player_name,
                "player_id": player_id,
                "npc_name": npc_name,
                "npc_id": npc_id,
                "kill_count": result.current,
                "milestone": milestone,
                "is_first_kill": result.is_first_kill,
                "previous_kc": result.previous,
                "image_url": image_url or "",
                "world_type": world_type,
                "plugin_version": plugin_version,
            },
            group_id,
            existing_session=external,
        )
