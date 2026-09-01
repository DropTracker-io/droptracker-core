"""Monthly recap delivery — who gets a card, when, and the record that they did.

The card itself is built by :mod:`services.recap` and rendered by
:mod:`services.recap_image`; this module decides *audience* and *timing*, and
writes the ledger row that makes both decisions auditable.

The audience rule, in one sentence: **every user receives exactly one
unsolicited recap — their first — and must opt in for the rest.** That needs no
per-user flag to go stale, because "have we ever sent this person one" is a
query against ``recap_deliveries``. A user who joined last month therefore hits
the same branch on their first eligible month that everyone hit on the very
first run, so "new players get a free one" is not a special case.

Clans work differently on purpose. Their first post is authorised by *seeding*
``recaps_enabled`` on a chosen cohort rather than by a first-free branch here, so
a clan can switch it off in advance and never receive one at all — which matters
more for a public channel than for a DM someone can ignore.

Timing has one sharp edge worth stating plainly. A recap cannot exist before its
month closes, and a month closes at **00:00 UTC** — that is when the leaderboard
partition rolls and when ``period_closed`` starts returning true. Local midnight
on the 1st in Auckland is 11:00 UTC on the 31st, when the month is still running.
So a subject's send time is the *later* of their local hour and the UTC rollover:
everyone at or behind UTC gets their real local time, and everyone ahead of it
gets the first moment the data can honestly exist. The pleasant side effect is
that a thousand DMs spread themselves across a day instead of arriving in one
burst against the same rate limit.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

from db.app_logger import AppLogger
from db.models.recap import (
    DELIVERY_CHANNEL,
    DELIVERY_DM,
    DELIVERY_FAILED,
    DELIVERY_FORBIDDEN,
    DELIVERY_NO_CARD,
    DELIVERY_SENT,
    SCOPE_GROUP,
    SCOPE_PLAYER,
    RecapDelivery,
    RecapSnapshot,
)

app_logger = AppLogger()

# Off until explicitly switched on, the same shape as ACTIVITY_DEEPLINK_ENABLED.
# Nothing in this module sends while it is false.
ENV_ENABLED = "RECAP_DELIVERY_ENABLED"
# While set, EVERY message — DMs and clan channel posts alike — goes to this one
# Discord user instead of its real recipient, prefixed with who it was for.
# Deliveries made this way are recorded with is_test=1 so they satisfy neither
# the idempotency check nor the one-free-recap entitlement.
ENV_TEST_TARGET = "RECAP_DELIVERY_TEST_DISCORD_ID"

# Each render spawns a headless chromium (~4s). Three at once keeps a full run
# to ~20 minutes without putting the box under memory pressure.
RENDER_CONCURRENCY = 3

# In test mode every message is re-addressed to one person, so a full month's run
# would put a thousand cards in that person's inbox. An unattended run (the
# systemd timer firing while the feature is being tested) therefore stops after
# this many; passing an explicit --limit overrides it, because then the size of
# the run was a decision rather than an accident.
TEST_MODE_TARGET_CAP = 5

# The hour a card goes out when nobody has chosen one, local to the subject's
# timezone. Noon rather than the month boundary: the leaderboards roll at 00:00
# UTC, but a recap arriving then reaches most of Europe and the Americas in the
# middle of the night, where it is buried by morning. A subject ahead of UTC can
# still be pulled back to the boundary — a card cannot exist before its month
# closes — but that lands in their afternoon, not their small hours.
DEFAULT_POST_HOUR = 12

# Config keys (mirrored in web_api/config_registry.py).
CFG_ENABLED = "recaps_enabled"
CFG_CHANNEL = "channel_id_to_post_recaps"
CFG_HOUR = "recap_post_hour"
CFG_TIMEZONE = "recap_timezone"
CFG_LOOTBOARD_CHANNEL = "lootboard_channel_id"

# A clan's card is *generated* at local midnight on the 1st, twelve hours before
# DEFAULT_POST_HOUR posts it. Group cards are not built on first view the way
# player cards are (web_api/routes/recaps.py generates only SCOPE_PLAYER), so
# before this pass a clan's /groups/{id}/recap/{period} URL 404'd until the
# delivery run happened to reach it — which for an America/Chicago clan meant
# 17:00 UTC. Generating at the day's own boundary means the archive URL is live
# for the whole of the 1st, and the noon post links to a card that already
# exists rather than being what brings it into being.
GENERATE_HOUR = 0

# User-level keys (mirrored in web_api/routes/me.py).
USER_CFG_OPT_IN = "dm_monthly_recap"
USER_CFG_TIMEZONE = "recap_timezone"
USER_CFG_DM_ISSUE = "dm_delivery_issue"


def delivery_enabled() -> bool:
    return os.getenv(ENV_ENABLED, "").strip().lower() in ("1", "true", "yes", "on")


def test_target() -> Optional[str]:
    value = (os.getenv(ENV_TEST_TARGET) or "").strip()
    return value or None


# --------------------------------------------------------------------------- #
# Timing (pure)
# --------------------------------------------------------------------------- #
def last_completed_month(now: Optional[datetime] = None) -> str:
    """The month a run on ``now`` should be sending cards for."""
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def month_close_utc(period: str) -> datetime:
    """The instant ``period`` finished: 00:00 UTC on the 1st of the next month.

    Nothing about a recap is true before this — the rollups are still receiving
    the month's last drops and ``services.recap.period_closed`` refuses.
    """
    year, month = int(period[:4]), int(period[5:7])
    return (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )


def _zone(tz_name: Optional[str]):
    """The named zone, or UTC. An unusable name is not worth failing a send
    over — the card arrives at the wrong hour rather than not at all — but it is
    worth saying so once."""
    if not tz_name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        app_logger.log(
            log_type="warning",
            data=f"unknown recap timezone {tz_name!r}; falling back to UTC",
            app_name="core",
            description="recap_delivery",
        )
        return timezone.utc


def due_at_utc(period: str, tz_name: Optional[str], hour: int) -> datetime:
    """When this subject's card should go out, in UTC.

    ``hour`` is local to ``tz_name`` on the 1st of the month after ``period``.
    Clamped forward to the month close, so a zone ahead of UTC cannot be
    scheduled into a moment when the month it summarises is still running.
    """
    close = month_close_utc(period)
    zone = _zone(tz_name)
    hour = max(0, min(23, int(hour or 0)))
    local_first = datetime(close.year, close.month, close.day, hour, tzinfo=zone)
    return max(local_first.astimezone(timezone.utc), close)


def is_due(
    now: datetime, period: str, tz_name: Optional[str], hour: int, *, grace_days: int = 3
) -> bool:
    """Whether ``now`` is inside this subject's send window.

    Open-ended would mean a bot that was down on the 1st quietly posting last
    month's card on the 20th; ``grace_days`` bounds the catch-up so a late run is
    still a *recap* rather than a surprise. The ledger, not this window, is what
    prevents a second send inside it.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    due = due_at_utc(period, tz_name, hour)
    return due <= now < due + timedelta(days=grace_days)


# --------------------------------------------------------------------------- #
# Audience (pure)
# --------------------------------------------------------------------------- #
def pick_best_account(accounts: Iterable[tuple[int, int]]) -> Optional[int]:
    """The account whose card a multi-account user should be sent.

    Their biggest month by loot, because that is the card they'd have picked.
    Ties break on the lower player id purely so a rerun chooses the same one.
    """
    best = None
    for player_id, loot in accounts:
        key = (-(loot or 0), int(player_id))
        if best is None or key < best[0]:
            best = (key, int(player_id))
    return best[1] if best else None


def user_is_entitled(*, opted_in: bool, had_prior: bool) -> bool:
    """One unsolicited recap, then only on request."""
    return opted_in or not had_prior


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
@dataclass
class GroupTarget:
    group_id: int
    name: str
    channel_id: str
    period: str


@dataclass
class UserTarget:
    user_id: int
    discord_id: str
    player_id: int
    player_name: str
    period: str
    # False when this is their first, unsolicited card — worth knowing at send
    # time because that message carries the opt-in buttons and the others don't.
    opted_in: bool = False


@dataclass
class DeliveryOutcome:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    # Counted apart from `failed` on purpose. A closed DM or a channel the bot
    # can't post in is a fact about the recipient, is recorded as delivered,
    # and is never retried — folding it into `failed` made a healthy launch run
    # report "failed=147" and left the exit status unable to tell a bad month
    # from a normal one.
    forbidden: int = 0
    notes: list[str] = field(default_factory=list)


def already_delivered(
    session, scope: str, subject_id: int, period: str, kind: str, is_test: bool
) -> bool:
    # `failed` means a fault on our side, which the model documents as
    # re-attemptable within the period — so a failed row does not settle the
    # subject; the next sweep picks them up again.
    return (
        session.query(RecapDelivery.id)
        .filter(
            RecapDelivery.scope == scope,
            RecapDelivery.subject_id == subject_id,
            RecapDelivery.period == period,
            RecapDelivery.kind == kind,
            RecapDelivery.is_test == (1 if is_test else 0),
            RecapDelivery.status != DELIVERY_FAILED,
        )
        .first()
        is not None
    )


def user_had_prior_recap(session, user_id: int) -> bool:
    """Whether this user has ever been sent a real recap DM.

    Test deliveries are excluded: a rollout that redirected their card to
    somebody else must not be counted as the card they were owed. Likewise
    `failed` (our fault, nothing arrived) and `no_card` (nothing to send that
    month) — only a send that reached Discord, or bounced off closed DMs by
    the recipient's own setting, consumes the one free recap.
    """
    return (
        session.query(RecapDelivery.id)
        .filter(
            RecapDelivery.kind == DELIVERY_DM,
            RecapDelivery.user_id == user_id,
            RecapDelivery.is_test == 0,
            RecapDelivery.status.in_((DELIVERY_SENT, DELIVERY_FORBIDDEN)),
        )
        .first()
        is not None
    )


def record_delivery(
    session,
    *,
    scope: str,
    subject_id: int,
    period: str,
    kind: str,
    status: str,
    user_id: Optional[int] = None,
    target_id: Optional[str] = None,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
    is_test: bool = False,
) -> RecapDelivery:
    # A re-attempt after a `failed` row must settle the same ledger slot, not
    # trip the (scope, subject, period, kind, is_test) unique constraint.
    row = (
        session.query(RecapDelivery)
        .filter(
            RecapDelivery.scope == scope,
            RecapDelivery.subject_id == subject_id,
            RecapDelivery.period == period,
            RecapDelivery.kind == kind,
            RecapDelivery.is_test == (1 if is_test else 0),
        )
        .first()
    )
    if row is None:
        row = RecapDelivery(
            scope=scope,
            subject_id=subject_id,
            period=period,
            kind=kind,
            is_test=1 if is_test else 0,
        )
        session.add(row)
    row.user_id = user_id
    row.target_id = str(target_id) if target_id else None
    row.status = status
    row.message_id = str(message_id) if message_id else None
    row.error = (error or None) and str(error)[:500]
    return row


def collect_group_targets(
    session, period: str, now: datetime, *, only_group: Optional[int] = None,
    is_test: bool = False, ignore_due: bool = False,
) -> list[GroupTarget]:
    """Clans whose card is due and not yet posted.

    Reads every group's four recap keys in one query rather than per group —
    ``utils.group_config.get_bulk`` exists for exactly this, and a per-group read
    over 250 clans is 1,000 round trips for data that fits in one.
    """
    from utils import group_config as gc

    rows = session.execute(
        text(
            "SELECT g.group_id, g.group_name FROM groups g "
            "WHERE g.group_id NOT IN (1, 2)"
            + (" AND g.group_id = :gid" if only_group else "")
        ),
        {"gid": only_group} if only_group else {},
    ).fetchall()
    if not rows:
        return []

    group_ids = [int(r[0]) for r in rows]
    names = {int(r[0]): r[1] for r in rows}
    cfg = gc.get_bulk(
        session,
        group_ids,
        [CFG_ENABLED, CFG_CHANNEL, CFG_HOUR, CFG_TIMEZONE, CFG_LOOTBOARD_CHANNEL],
    )

    out: list[GroupTarget] = []
    for group_id in group_ids:
        # get_bulk keys on (group_id, key) and omits absent rows entirely.
        def value(key: str) -> Optional[str]:
            return cfg.get((group_id, key))

        if not gc.is_truthy(value(CFG_ENABLED)):
            continue
        # Explicit recap channel, else the lootboard channel — a clan's monthly
        # totals already live there, so most groups never set the first.
        channel = (value(CFG_CHANNEL) or value(CFG_LOOTBOARD_CHANNEL) or "").strip()
        if not channel:
            continue
        raw_hour = value(CFG_HOUR)
        try:
            # Unset (the seeded cohort has no row) means the default, not 0.
            hour = DEFAULT_POST_HOUR if raw_hour in (None, "") else int(raw_hour)
        except (TypeError, ValueError):
            hour = DEFAULT_POST_HOUR
        if not ignore_due and not is_due(now, period, value(CFG_TIMEZONE), hour):
            continue
        if already_delivered(session, SCOPE_GROUP, group_id, period, DELIVERY_CHANNEL, is_test):
            continue
        out.append(
            GroupTarget(
                group_id=group_id,
                name=names.get(group_id) or f"Group {group_id}",
                channel_id=channel,
                period=period,
            )
        )
    return out


def collect_user_targets(
    session, period: str, now: datetime, *, only_user: Optional[int] = None,
    limit: Optional[int] = None, is_test: bool = False, ignore_due: bool = False,
) -> list[UserTarget]:
    """Users owed a recap DM for ``period``.

    Walks players active in the period rather than the whole user table: the
    rollup is the only cheap source of "did anything happen for this account",
    and roughly five in eight active players are unclaimed plugin installs with
    no Discord user to send anything to.
    """
    from services.recap import _redis_totals, period_partition
    from web_api.common import hidden_player_ids

    # Nobody can be due before the month closes, whatever their timezone says.
    # Checked first because everything below is a multi-second scan of the
    # rollup, and a sweep running every fifteen minutes shouldn't pay for it
    # just to learn there is nothing to do.
    if not ignore_due and now < month_close_utc(period):
        return []

    partition = period_partition(period)
    year, month = partition // 100, partition % 100
    lo, hi = f"{year:04d}-{month:02d}-01-00", f"{year:04d}-{month:02d}-31-23"

    # Claimed, visible accounts with activity in the period, and their owner.
    rows = session.execute(
        text(
            "SELECT DISTINCT p.player_id, p.player_name, u.user_id, u.discord_id, "
            "       COALESCE(u.never_ping, 0) AS never_ping "
            "FROM player_item_hourly_totals r "
            "JOIN players p ON p.player_id = r.player_id "
            "JOIN users u ON u.user_id = p.user_id "
            "WHERE r.date_hour BETWEEN :lo AND :hi "
            "  AND u.discord_id IS NOT NULL AND u.discord_id <> '' AND u.discord_id <> '0' "
            "  AND COALESCE(p.hidden, 0) = 0 AND COALESCE(u.hidden, 0) = 0"
            + (" AND u.user_id = :uid" if only_user else "")
        ),
        {"lo": lo, "hi": hi, **({"uid": only_user} if only_user else {})},
    ).fetchall()
    if not rows:
        return []

    hidden = set()
    try:
        hidden = set(hidden_player_ids())
    except Exception:
        # Fail closed on the privacy list: better to send nothing this cycle
        # than to send a card for someone who asked not to be seen.
        app_logger.log(
            log_type="error",
            data="could not read hidden player ids; skipping recap DM cycle",
            app_name="core",
            description="recap_delivery",
        )
        return []

    by_user: dict[int, dict] = {}
    for player_id, player_name, user_id, discord_id, never_ping in rows:
        player_id, user_id = int(player_id), int(user_id)
        if player_id in hidden or int(never_ping or 0):
            continue
        entry = by_user.setdefault(
            user_id, {"discord_id": str(discord_id), "players": {}}
        )
        entry["players"][player_id] = player_name

    if not by_user:
        return []

    # One Redis pass for every candidate account's month, so the best-account
    # choice costs nothing per user.
    all_players = [pid for e in by_user.values() for pid in e["players"]]
    totals = _redis_totals(all_players, partition)

    prefs = _user_prefs(session, list(by_user))

    out: list[UserTarget] = []
    for user_id, entry in by_user.items():
        opted_in, tz_name = prefs.get(user_id, (False, None))
        # Their own noon, from the timezone the site seeded on their first visit
        # — a recap landing at 3am is one nobody reads.
        if not ignore_due and not is_due(now, period, tz_name, DEFAULT_POST_HOUR):
            continue
        if not user_is_entitled(
            opted_in=opted_in, had_prior=user_had_prior_recap(session, user_id)
        ):
            continue
        best = pick_best_account(
            (pid, int(totals.get(pid, 0))) for pid in entry["players"]
        )
        if best is None:
            continue
        if already_delivered(session, SCOPE_PLAYER, best, period, DELIVERY_DM, is_test):
            continue
        out.append(
            UserTarget(
                user_id=user_id,
                discord_id=entry["discord_id"],
                player_id=best,
                player_name=entry["players"].get(best) or f"Player {best}",
                period=period,
                opted_in=opted_in,
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def _user_prefs(session, user_ids: list[int]) -> dict[int, tuple[bool, Optional[str]]]:
    """``{user_id: (opted_in, timezone)}`` — both keys in one read.

    Absent rows are the common case (opt-in defaults off, and a timezone only
    exists once someone has opened the site since the feature shipped), so
    callers get the defaults rather than a missing key.
    """
    if not user_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT user_id, config_key, config_value FROM user_configurations "
            "WHERE config_key IN (:opt, :tz) AND user_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"opt": USER_CFG_OPT_IN, "tz": USER_CFG_TIMEZONE, "ids": user_ids},
    ).fetchall()
    out: dict[int, tuple[bool, Optional[str]]] = {}
    for user_id, key, value in rows:
        user_id = int(user_id)
        opted_in, tz_name = out.get(user_id, (False, None))
        if key == USER_CFG_OPT_IN:
            opted_in = str(value).strip().lower() in ("1", "true", "yes", "on")
        else:
            tz_name = (value or "").strip() or None
        out[user_id] = (opted_in, tz_name)
    return out


# --------------------------------------------------------------------------- #
# Pre-generation (no sending)
# --------------------------------------------------------------------------- #
def _safe_rollback(session) -> None:
    """Roll back if this session can be — the recovery path must not raise its
    own exception over the one it is recovering from."""
    try:
        session.rollback()
    except Exception:
        pass


def collect_group_generation_ids(
    session, period: str, now: datetime, *,
    only_group: Optional[int] = None, ignore_due: bool = False,
) -> list[int]:
    """Clans whose card should already exist, newest-eligible first.

    Deliberately looser than :func:`collect_group_targets`, because generating
    is not delivering:

    * no channel requirement — a clan with nowhere to post still has a public
      archive URL, and that URL is the whole point of this pass;
    * no ``already_delivered`` check — the ledger records *posts*, and a clan
      posted to last month says nothing about this month's card existing;
    * no upper bound on the window. ``is_due`` bounds delivery by
      ``grace_days`` so a bot that was down on the 1st cannot surprise a clan
      with a post on the 20th. Nothing is surprised by a card that merely
      exists, and :func:`ensure_snapshot` computes each one exactly once, so
      the gate here is only "has this clan's local 1st begun".

    ``recaps_enabled`` still gates it: a clan that switched recaps off has said
    it does not want the feature, and computing 250 cards nobody asked for is
    the cost this avoids.
    """
    from utils import group_config as gc

    rows = session.execute(
        text(
            "SELECT g.group_id FROM groups g WHERE g.group_id NOT IN (1, 2)"
            + (" AND g.group_id = :gid" if only_group else "")
        ),
        {"gid": only_group} if only_group else {},
    ).fetchall()
    if not rows:
        return []

    group_ids = [int(r[0]) for r in rows]
    cfg = gc.get_bulk(session, group_ids, [CFG_ENABLED, CFG_TIMEZONE])

    out: list[int] = []
    for group_id in group_ids:
        if not gc.is_truthy(cfg.get((group_id, CFG_ENABLED))):
            continue
        if not ignore_due:
            # GENERATE_HOUR, never CFG_HOUR: moving the post time must not move
            # when the archive goes live, or a clan that posts at 20:00 would
            # keep its own card hidden all day.
            if due_at_utc(period, cfg.get((group_id, CFG_TIMEZONE)), GENERATE_HOUR) > now:
                continue
        out.append(group_id)
    return out


async def generate_group_cards(
    session, *, period: str, now: datetime, apply: bool = False,
    only_group: Optional[int] = None, ignore_due: bool = False, log=print,
) -> tuple[int, int]:
    """Build every eligible clan card that does not exist yet.

    Returns ``(built, eligible)``. Sends nothing and renders no image — the
    delivery pass still owns both, and reuses whatever this leaves behind.

    A dry run reports and writes nothing, matching the rest of the script.
    """
    eligible = collect_group_generation_ids(
        session, period, now, only_group=only_group, ignore_due=ignore_due
    )
    missing = [
        group_id for group_id in eligible
        if not snapshot_exists(session, SCOPE_GROUP, group_id, period)
    ]
    if not missing:
        return 0, len(eligible)

    if not apply:
        log(f"  would generate {len(missing)} clan card(s) for {period}")
        return 0, len(eligible)

    # Before the compute, never after: EHB is the one figure on the card that
    # comes from outside, ``ensure_snapshot`` returns an existing row untouched,
    # and the noon post reuses whatever is stored. Harvesting afterwards would
    # freeze an EHB-less card into the archive AND into the message.
    try:
        from services.recap_ehb import harvest_month_ehb

        await harvest_month_ehb(session, period, group_ids=missing, player_ids=[], log=log)
    except Exception as e:
        log(f"  EHB harvest unavailable for generation: {e}")

    built = 0
    for group_id in missing:
        try:
            if ensure_snapshot(session, SCOPE_GROUP, group_id, period):
                built += 1
        except Exception as e:
            # One clan's bad roster must not cost every later clan its card.
            _safe_rollback(session)
            app_logger.log(
                log_type="error",
                data=f"recap pre-generation failed for group {group_id} {period}: {e}",
                app_name="core",
                description="recap_delivery",
            )
    log(f"  generated {built}/{len(missing)} clan card(s) for {period}")
    return built, len(eligible)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def snapshot_exists(session, scope: str, subject_id: int, period: str) -> bool:
    """Whether the card is already stored — the cheap check the pre-generation
    pass makes per clan, so an existing card costs a key lookup, not a compute."""
    return (
        session.query(RecapSnapshot.id)
        .filter(
            RecapSnapshot.scope == scope,
            RecapSnapshot.subject_id == subject_id,
            RecapSnapshot.period == period,
        )
        .first()
        is not None
    )


def ensure_snapshot(session, scope: str, subject_id: int, period: str) -> Optional[str]:
    """The card's ``generated_at`` stamp, computing the card if it doesn't exist.

    Player cards are not pre-generated — there are thousands of active players a
    month and almost none of those cards would ever be opened — so the delivery
    run builds what its audience needs and nothing else. That inverts the usual
    order: selection first, generation second.

    ``None`` means there is no card to send, which is the normal answer for a
    subject below the activity floor or one who has opted out of public display.
    """
    row = (
        session.query(RecapSnapshot)
        .filter(
            RecapSnapshot.scope == scope,
            RecapSnapshot.subject_id == subject_id,
            RecapSnapshot.period == period,
        )
        .first()
    )
    if row:
        return row.generated_at.isoformat() if row.generated_at else period

    from services.recap import (
        RosterTooLarge,
        compute_group_month,
        compute_player_month,
        period_closed,
        save_snapshot,
    )

    if not period_closed(period):
        return None
    try:
        payload = (
            compute_group_month(session, subject_id, period)
            if scope == SCOPE_GROUP
            else compute_player_month(session, subject_id, period)
        )
    except RosterTooLarge as e:
        app_logger.log(
            log_type="warning",
            data=f"recap delivery skipped {scope} {subject_id}: {e}",
            app_name="core",
            description="recap_delivery",
        )
        return None
    if not payload:
        return None
    row = save_snapshot(session, scope, subject_id, period, payload)
    session.commit()
    return row.generated_at.isoformat() if row.generated_at else period


def group_lootboard_path(group_id: int, period: str) -> str:
    """Where the month's lootboard is frozen for a clan's recap.

    Deliberately not the generator's own ``lb/timeframes/`` filename: that name
    encodes exact timestamps and belongs to the on-demand board feature, which
    is free to prune or rename it. A recap message may be read for years, so its
    image gets a stable path of its own beside the card.
    """
    return (
        f"/store/droptracker/disc/static/assets/img/clans/{group_id}"
        f"/recap/lootboard-{period}.png"
    )


def group_lootboard_url(group_id: int, period: str) -> str:
    return f"{_SITE}/img/clans/{group_id}/recap/lootboard-{period}.png"


async def ensure_group_lootboard(group_id: int, period: str) -> Optional[str]:
    """The clan's lootboard for ``period``, generating it once and keeping it.

    This is the graphical board members already know from their own channel —
    the recap's "monthly leaderboard" button shows that, not a rewritten list.
    Rendered for the month's exact bounds rather than reusing the live board,
    which is continuously overwritten and by the time anyone presses the button
    would show a different month entirely.
    """
    path = group_lootboard_path(group_id, period)
    if os.path.exists(path):
        return group_lootboard_url(group_id, period)

    import calendar
    import shutil

    year, month = int(period[:4]), int(period[5:7])
    last_day = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1)
    end = datetime(year, month, last_day, 23, 59, 59)

    try:
        from lootboard.generator import generate_timeframe_board

        generated = await generate_timeframe_board(
            group_id=group_id, start_time=start, end_time=end
        )
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"recap lootboard generation failed for group {group_id} {period}: {e}",
            app_name="core",
            description="recap_delivery",
        )
        return None
    if not generated or not os.path.exists(generated):
        return None

    try:
        from lootboard.generator import _ensure_public_dir

        _ensure_public_dir(os.path.dirname(path))
        # Copy rather than move: the generator's own file backs the on-demand
        # board feature and isn't ours to take.
        tmp = f"{path}.tmp"
        shutil.copyfile(generated, tmp)
        os.replace(tmp, path)
        os.chmod(path, 0o666)
    except OSError as e:
        app_logger.log(
            log_type="error",
            data=f"could not store recap lootboard {path}: {e}",
            app_name="core",
            description="recap_delivery",
        )
        return None
    return group_lootboard_url(group_id, period)


async def render_all(
    stamps: dict[tuple[str, int], str], period: str
) -> dict[tuple[str, int], str]:
    """Render many cards concurrently, returning their public image URLs.

    Takes stamps rather than a session on purpose: every card is computed and
    committed *before* this runs, so nothing here touches the database while
    several coroutines are in flight. Each render is a headless chromium
    process, so the semaphore is the one thing standing between a monthly run
    and the box's memory.
    """
    from services.recap_image import write_recap_image

    sem = asyncio.Semaphore(RENDER_CONCURRENCY)
    out: dict[tuple[str, int], str] = {}

    async def one(key: tuple[str, int], stamp: str):
        scope, subject_id = key
        async with sem:
            url = await write_recap_image(scope, subject_id, period, stamp)
            if url:
                out[key] = url

    await asyncio.gather(*(one(k, s) for k, s in stamps.items()))
    return out


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
_GOLD = 0xC8A24C
_SITE = "https://www.droptracker.io"

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def format_period(period: str) -> str:
    if len(period) == 4:
        return period
    year, month = period[:4], int(period[5:7])
    return f"{_MONTHS[month - 1]} {year}"


def month_phrase(period: str, this_year: Optional[int] = None) -> str:
    """How a message refers to the period in a sentence.

    "the month of July" reads better than "the month of July 2026" for the card
    that arrives days later, but a re-send of an older month has to say which
    year or it's just wrong. So the year appears only when it isn't the current
    one.
    """
    if len(period) == 4:
        return period
    year, month = int(period[:4]), int(period[5:7])
    name = _MONTHS[month - 1]
    this_year = this_year if this_year is not None else datetime.now(timezone.utc).year
    return name if year == this_year else f"{name} {year}"


def _gp(value: int) -> str:
    """Short GP, matching how the card itself writes numbers."""
    value = int(value or 0)
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(value) >= size:
            return f"{value / size:.2f}{unit}"
    return str(value)


def player_recap_url(player_id: int, period: str) -> str:
    return f"{_SITE}/players/{player_id}/recap/{period}"


def group_recap_url(group_id: int, period: str) -> str:
    return f"{_SITE}/groups/{group_id}/recap/{period}"


def _summary_line(payload: dict) -> str:
    """One line of numbers under the title.

    Kept to what the payload can always answer — the image carries the detail,
    and a line that says "0 drops" because a source wasn't captured would
    undercut the card it introduces.
    """
    totals = (payload or {}).get("totals") or {}
    rank = (payload or {}).get("rank") or {}
    bits = []
    loot = totals.get("loot") or totals.get("loot_rollup") or 0
    if loot:
        bits.append(f"**{_gp(loot)}** looted")
    if totals.get("drops"):
        bits.append(f"{int(totals['drops']):,} drops")
    if totals.get("ehb"):
        bits.append(f"{float(totals['ehb']):,.1f} EHB")
    if rank.get("position") and rank.get("of"):
        bits.append(f"ranked {int(rank['position']):,} of {int(rank['of']):,}")
    return " · ".join(bits)


def build_dm_message(target: UserTarget, payload: dict, image_url: Optional[str]) -> dict:
    """The player's own card, plus the two buttons that decide whether they get
    another one.

    A first, unsolicited card offers "keep sending these" and "no thanks"; a
    card someone asked for offers only the way out. Nobody should have to
    re-confirm a choice they already made.
    """
    url = player_recap_url(target.player_id, target.period)
    embed = {
        "title": f"{target.player_name} — {format_period(target.period)}",
        "url": url,
        "color": _GOLD,
        "footer": {"text": "DropTracker.io"},
    }
    summary = _summary_line(payload)
    if summary:
        embed["description"] = summary
    if image_url:
        embed["image"] = {"url": image_url}

    # Addressed to the person, not announced at them: their name, their account,
    # the month by name. `-#` is Discord's subtext, which is where the
    # housekeeping belongs — the card is the message, the opt-in is a footnote.
    greeting = (
        f"Hey, <@{target.discord_id}>!\n"
        f"We recapped everything you tracked for the month of "
        f"**{month_phrase(target.period)}** on **{target.player_name}**!"
    )

    if target.opted_in:
        buttons = [
            {"type": 2, "style": 2, "label": "Stop sending these", "custom_id": "recap_optin:off"},
        ]
        # No footnote on a card they asked for: the only thing left to say is how
        # to stop, and the button already says it.
        content = greeting
    else:
        buttons = [
            {"type": 2, "style": 1, "label": "Keep sending these", "custom_id": "recap_optin:on"},
            {"type": 2, "style": 2, "label": "No thanks", "custom_id": "recap_optin:off"},
        ]
        content = (
            f"{greeting}\n"
            "-# P.S. **If you'd like to keep receiving these each month**, press the "
            "button below — otherwise we'll only send this one, and you can view them "
            "yourself on your profile from here on out!"
        )
    buttons.append({"type": 2, "style": 5, "label": "View on the site", "url": url})

    return {"content": content, "embeds": [embed], "components": [{"type": 1, "components": buttons}]}


def build_channel_message(target: GroupTarget, payload: dict, image_url: Optional[str]) -> dict:
    """The clan's card. No opt-in buttons: a channel post is not one person's to
    decide, so the switch lives in group settings where an admin can reach it."""
    url = group_recap_url(target.group_id, target.period)
    embed = {
        "title": f"{target.name} — {format_period(target.period)}",
        "url": url,
        "color": _GOLD,
        # Says what the default actually is: one card, unless a clan asks for
        # more. "Manage recaps" implied they were already subscribed.
        "footer": {
            "text": "DropTracker.io · configure receiving recaps every month "
                    "in your group settings"
        },
    }
    summary = _summary_line(payload)
    if summary:
        embed["description"] = summary
    if image_url:
        embed["image"] = {"url": image_url}
    return {
        # A heading and nothing else. The card carries the story, and a channel
        # that already sees every drop doesn't need it narrated — the one line of
        # housekeeping (where to configure this) stays in the embed footer.
        "content": f"# {target.name} — {month_phrase(target.period)} in review",
        "embeds": [embed],
        "components": [
            {"type": 1, "components": [
                # Answers "where did I come?" privately — the card only has room
                # for the top five, and a channel doesn't need everyone's answer
                # posted in it. Handled by services/recap_buttons.py.
                {
                    "type": 2, "style": 2, "label": "Monthly leaderboard",
                    "custom_id": f"recap_lb:{target.group_id}:{target.period}",
                },
                {"type": 2, "style": 5, "label": "View on the site", "url": url},
            ]}
        ],
    }


def with_test_banner(message: dict, description: str) -> dict:
    """Re-address a message to the test recipient, saying who it was for.

    Without the banner a rollout run is a pile of identical cards with no way to
    tell whether routing was right, which is the one thing the run is for.
    """
    out = dict(message)
    prefix = f"🧪 **Test** — this would have gone to {description}."
    out["content"] = f"{prefix}\n{message['content']}" if message.get("content") else prefix
    return out


def set_dm_delivery_issue(session, user_id: int, value: bool = True) -> None:
    """Raise the flag the website already shows as "your DMs are closed".

    A bounced recap is deliberately *not* retried and still counts as delivered
    (see the ledger model), so this banner is the only feedback loop left: the
    user fixes their Discord privacy settings and opts in again.
    """
    session.execute(
        text(
            "INSERT INTO user_configurations (user_id, config_key, config_value) "
            "VALUES (:uid, :key, :val) "
            "ON DUPLICATE KEY UPDATE config_value = :val"
        ),
        {"uid": user_id, "key": USER_CFG_DM_ISSUE, "val": "true" if value else "false"},
    )


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _describe(target) -> str:
    if isinstance(target, GroupTarget):
        return f"**{target.name}** in channel `{target.channel_id}`"
    return f"<@{target.discord_id}> (**{target.player_name}**)"


async def deliver_one(
    session,
    target,
    *,
    rest,
    writer,
    payload: dict,
    image_url: Optional[str],
    test_discord_id: Optional[str],
    apply: bool,
) -> str:
    """Send one card and record what happened. Returns a status string.

    Every exit path writes a ledger row except the dry run, because a send we
    can't account for is worse than one we didn't make: the next cycle would
    either repeat it or skip someone who never got theirs.
    """
    is_group = isinstance(target, GroupTarget)
    scope = SCOPE_GROUP if is_group else SCOPE_PLAYER
    subject_id = target.group_id if is_group else target.player_id
    kind = DELIVERY_CHANNEL if is_group else DELIVERY_DM
    user_id = None if is_group else target.user_id

    message = (
        build_channel_message(target, payload, image_url)
        if is_group
        else build_dm_message(target, payload, image_url)
    )
    if test_discord_id:
        message = with_test_banner(message, _describe(target))

    if not apply:
        return "planned"

    # In test mode everything becomes a DM to one person, including what would
    # have been a clan channel post — the routing is what's under test, not the
    # destination.
    if test_discord_id:
        bucket = f"dm:{test_discord_id}"

        async def send():
            return await rest.send_dm(test_discord_id, message)
    elif is_group:
        bucket = f"channel:{target.channel_id}"

        async def send():
            return await rest.post_message(target.channel_id, message)
    else:
        bucket = f"dm:{target.discord_id}"

        async def send():
            return await rest.send_dm(target.discord_id, message)

    from interactions.client.errors import Forbidden, NotFound

    try:
        message_id = await writer.write(bucket, send, expect_result=True)
    except (Forbidden, NotFound) as e:
        # Closed DMs (or a deleted channel). Recorded as delivered on purpose:
        # retrying every month would produce the same bounce forever for people
        # who never interact. The website's existing banner is the feedback loop.
        record_delivery(
            session, scope=scope, subject_id=subject_id, period=target.period,
            kind=kind, status=DELIVERY_FORBIDDEN, user_id=user_id,
            target_id=test_discord_id or (target.channel_id if is_group else target.discord_id),
            error=str(e), is_test=bool(test_discord_id),
        )
        if user_id and not test_discord_id:
            set_dm_delivery_issue(session, user_id, True)
        session.commit()
        return "forbidden"
    except Exception as e:
        record_delivery(
            session, scope=scope, subject_id=subject_id, period=target.period,
            kind=kind, status=DELIVERY_FAILED, user_id=user_id,
            target_id=test_discord_id or (target.channel_id if is_group else target.discord_id),
            error=str(e), is_test=bool(test_discord_id),
        )
        session.commit()
        return "failed"

    record_delivery(
        session, scope=scope, subject_id=subject_id, period=target.period,
        kind=kind, status=DELIVERY_SENT, user_id=user_id,
        target_id=test_discord_id or (target.channel_id if is_group else target.discord_id),
        message_id=message_id, is_test=bool(test_discord_id),
    )
    # A successful DM clears the "your DMs are closed" banner, the same way the
    # submission-DM path does.
    if user_id and not test_discord_id:
        set_dm_delivery_issue(session, user_id, False)
    session.commit()
    return "sent"


async def run_delivery(
    session,
    *,
    period: str,
    now: Optional[datetime] = None,
    apply: bool = False,
    only_group: Optional[int] = None,
    only_user: Optional[int] = None,
    limit: Optional[int] = None,
    ignore_due: bool = False,
    include_groups: bool = True,
    include_users: bool = True,
    log=print,
) -> DeliveryOutcome:
    """One delivery pass: select, render, send, record.

    Refuses to send unless ``RECAP_DELIVERY_ENABLED`` is set — a dry run still
    works without it, so the selection can be inspected safely at any time.
    """
    now = now or datetime.now(timezone.utc)
    test_id = test_target()
    outcome = DeliveryOutcome()

    if apply and not delivery_enabled():
        outcome.notes.append(f"{ENV_ENABLED} is not set — refusing to send.")
        return outcome

    # Generation first, and outside the "is anything due to send" question:
    # local midnight is twelve hours before the default post hour, so on most
    # ticks of the 1st this pass is the only work there is. Running it after the
    # `not targets` early return would mean a clan's URL went live at the same
    # moment as its post, which is the problem this exists to fix.
    if include_groups:
        try:
            await generate_group_cards(
                session, period=period, now=now, apply=apply,
                only_group=only_group, ignore_due=ignore_due, log=log,
            )
        except Exception as e:
            # Delivery is the job that must not be lost; a failed pre-generation
            # only costs the archive its head start, since ensure_snapshot below
            # still builds each card on the way out.
            _safe_rollback(session)
            log(f"  clan card pre-generation failed: {e}")

    targets: list = []
    if include_groups:
        targets += collect_group_targets(
            session, period, now, only_group=only_group,
            is_test=bool(test_id), ignore_due=ignore_due,
        )
    if include_users:
        targets += collect_user_targets(
            session, period, now, only_user=only_user, limit=limit,
            is_test=bool(test_id), ignore_due=ignore_due,
        )
    if not targets:
        outcome.notes.append("nothing due")
        return outcome

    if test_id and limit is None and len(targets) > TEST_MODE_TARGET_CAP:
        dropped = len(targets) - TEST_MODE_TARGET_CAP
        targets = targets[:TEST_MODE_TARGET_CAP]
        outcome.notes.append(
            f"test mode: capped at {TEST_MODE_TARGET_CAP} targets ({dropped} not sent) — "
            "pass --limit to override"
        )

    log(f"  {len(targets)} target(s) due for {period}")

    # EHB lives at Wise Old Man, and the card computation is synchronous and
    # must never call out — so the month's figures are fetched here, for this
    # audience, before a single card is built. Only the first tick of the
    # delivery window pays: a closed month is harvested once and kept.
    try:
        from services.recap_ehb import harvest_month_ehb

        await harvest_month_ehb(
            session,
            period,
            group_ids=[t.group_id for t in targets if isinstance(t, GroupTarget)],
            player_ids=[t.player_id for t in targets if isinstance(t, UserTarget)],
            log=log,
        )
    except Exception as e:
        # A harvest that cannot run costs the cards one stat, not the run.
        log(f"  EHB harvest unavailable: {e}")

    # Build every card first, then render them, then send: a subject whose card
    # can't be produced loses their message rather than derailing the run, and
    # the compute pass finishes its database work before any concurrency starts.
    stamps: dict[tuple[str, int], str] = {}
    for target in targets:
        is_group = isinstance(target, GroupTarget)
        key = (
            SCOPE_GROUP if is_group else SCOPE_PLAYER,
            target.group_id if is_group else target.player_id,
        )
        stamp = ensure_snapshot(session, key[0], key[1], period)
        if stamp:
            stamps[key] = stamp
    log(f"  {len(stamps)}/{len(targets)} card(s) available")

    images = await render_all(stamps, period)
    log(f"  {len(images)}/{len(stamps)} card image(s) rendered")

    # Freeze each clan's monthly lootboard now, so the button on the post is
    # instant rather than making whoever presses it wait on a render.
    boards = 0
    for target in targets:
        if isinstance(target, GroupTarget):
            if await ensure_group_lootboard(target.group_id, period):
                boards += 1
    if boards:
        log(f"  {boards} monthly lootboard(s) ready")

    from services.recap import load_snapshot
    from utils.discord_rest import DiscordRest
    from utils.discord_write import DiscordWriter

    token = os.getenv("BOT_TOKEN", "")
    writer = DiscordWriter(
        label="recaps",
        # Comfortably inside Discord's global ceiling, and slow enough that a
        # thousand DMs never look like a raid to anti-spam.
        global_max_calls=4, global_period=1.0,
        bucket_max_calls=1, bucket_period=1.0,
    )

    async def _run(rest):
        for target in targets:
            is_group = isinstance(target, GroupTarget)
            scope = SCOPE_GROUP if is_group else SCOPE_PLAYER
            subject_id = target.group_id if is_group else target.player_id
            payload = load_snapshot(session, scope, subject_id, period) or {}
            image_url = images.get((scope, subject_id))
            if not payload:
                # Settle the ledger slot so the sweep stops re-planning a
                # subject that will not have a card this period.
                if apply:
                    record_delivery(
                        session, scope=scope, subject_id=subject_id,
                        period=target.period,
                        kind=DELIVERY_CHANNEL if is_group else DELIVERY_DM,
                        status=DELIVERY_NO_CARD,
                        user_id=None if is_group else target.user_id,
                        is_test=bool(test_id),
                    )
                    session.commit()
                outcome.skipped += 1
                log(f"  skip {_describe(target)}: no snapshot")
                continue
            status = await deliver_one(
                session, target, rest=rest, writer=writer, payload=payload,
                image_url=image_url, test_discord_id=test_id, apply=apply,
            )
            if status == "sent":
                outcome.sent += 1
            elif status == "planned":
                outcome.skipped += 1
            elif status == "forbidden":
                outcome.forbidden += 1
            else:
                outcome.failed += 1
            log(f"  {status:9} {_describe(target)}")

    if apply:
        async with DiscordRest(token) as rest:
            await _run(rest)
    else:
        await _run(None)

    if test_id:
        outcome.notes.append(f"test mode: everything addressed to {test_id}")
    return outcome
