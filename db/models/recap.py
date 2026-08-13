"""Recap snapshot ORM model (monthly / annual "Wrapped").

One row per (scope, subject, period) holding a fully-computed recap card as
JSON. The snapshot *is* the artifact — pages, images and Discord posts all read
it rather than recomputing, which is what makes a permanent public URL cheap.

Why persisted and not cached:

* the annual recap is a fold over twelve monthly rows, so those rows must
  outlive any TTL;
* ``/groups/{id}/recap/{period}`` is meant to stay valid forever;
* the biggest-drop card points at a screenshot that
  ``droptracker-prune-images.timer`` would otherwise delete once it is 30 days
  old and worth under 1M GP. Freezing the payload captures the URL; the prune
  script reads these payloads back (``recap_protected_paths``) and skips the
  files they name, so the two together keep the card whole.

``period`` is ``'YYYY-MM'`` for a month and ``'YYYY'`` for a year. Both sort
chronologically as strings, and ``len(period)`` distinguishes them, so no
separate "kind" column is needed.

``schema_version`` exists so later versions can add cards without invalidating
old rows. Recaps are an archive: add cards, never swap them out — a reader that
finds a missing key on an old row should omit that card, not fail.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from .base import Base

# Bump when the payload shape changes in a way readers must notice.
RECAP_SCHEMA_VERSION = 1

SCOPE_GROUP = "group"
SCOPE_PLAYER = "player"
RECAP_SCOPES = (SCOPE_GROUP, SCOPE_PLAYER)

# `recap_snapshots` is shared with the Clan Log, which stores its boards under
# scope `clan_log` (see db/models/clan_log.SCOPE_CLAN_LOG). Every reader and
# sweep here therefore has to keep filtering on an explicit scope — a query that
# selects "all snapshots for period X" without one will pick up boards that are
# not recaps and have a different payload shape.

# How a card reached its audience.
DELIVERY_DM = "dm"
DELIVERY_CHANNEL = "channel"
DELIVERY_KINDS = (DELIVERY_DM, DELIVERY_CHANNEL)

# Terminal states. `forbidden` is deliberately NOT a failure to retry: a user
# whose DMs are closed is told on the website that they need to open them, and
# the row still counts as delivered, so we don't re-attempt a bounce every month
# for people who never interact. `failed` is for faults on our side (render
# broke, Discord 5xx after retries) and may be re-attempted within the period.
# `no_card` records a subject that was due but had nothing to send (below the
# activity floor, opted out of public display) — it stops the delivery sweep
# re-planning them every cycle, without counting as the recap they were owed.
DELIVERY_SENT = "sent"
DELIVERY_FORBIDDEN = "forbidden"
DELIVERY_FAILED = "failed"
DELIVERY_NO_CARD = "no_card"
DELIVERY_STATUSES = (
    DELIVERY_SENT, DELIVERY_FORBIDDEN, DELIVERY_FAILED, DELIVERY_NO_CARD
)


class RecapSnapshot(Base):
    """A computed recap card for one subject over one period."""

    __tablename__ = "recap_snapshots"
    __table_args__ = (
        # The idempotency guard: re-running a cycle for a period it has already
        # done updates in place rather than inserting a duplicate.
        UniqueConstraint(
            "scope", "subject_id", "period", name="uq_recap_scope_subject_period"
        ),
        # "every group's 2026-07" for the cycle sweep; the per-subject archive
        # index reads the same index from the other direction.
        Index("idx_recap_period_scope", "period", "scope"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Plain string rather than an ENUM so adding a scope stays a code change
    # instead of a table rebuild.
    scope = Column(String(16), nullable=False)
    subject_id = Column(Integer, nullable=False)
    period = Column(String(7), nullable=False)
    payload = Column(LONGTEXT, nullable=False)
    schema_version = Column(
        Integer, nullable=False, default=RECAP_SCHEMA_VERSION, server_default="1"
    )
    generated_at = Column(DateTime, nullable=False, server_default=func.now())


class RecapWomGain(Base):
    """One player's EHB gained over one month, as reported by WOM.

    Not derivable from anything else we store: ``players.ehb`` is a lifetime
    total overwritten on every roster sync, so it holds no history to
    difference. The month's gain is harvested once, after the month closes,
    and kept — a closed month's gains never change, so this is a permanent
    cache rather than something to refresh.

    That permanence is what makes the card's month-over-month comparison free:
    the "previous month" figure was written by last month's run. It is also
    what keeps the lazily-generated player cards off the WOM rate limiter,
    since their clan's harvest has usually already covered them.

    ``source`` records which call produced the row — ``'bulk'`` for the
    per-clan ``bulk-gained`` fetch that covers a whole roster in one request,
    ``'player'`` for the per-player fallback used by someone in no WOM-linked
    group. Absence of a row means "not harvested", which the card treats as
    unknown and omits; it never means zero.
    """

    __tablename__ = "recap_wom_gains"
    __table_args__ = (
        Index("idx_recap_wom_gains_period", "period"),
        {"extend_existing": True},
    )

    player_id = Column(Integer, primary_key=True)
    period = Column(String(7), primary_key=True)
    ehb_gained = Column(Float, nullable=False, default=0.0, server_default="0")
    source = Column(String(16), nullable=False, default="bulk", server_default="bulk")
    fetched_at = Column(DateTime, nullable=False, server_default=func.now())


class RecapDelivery(Base):
    """One record of a recap card reaching (or failing to reach) an audience.

    Distinct from :class:`RecapSnapshot`, which records that a card was *built*.
    This records that it was *sent*, and it answers two different questions:

    *Idempotency.* The unique constraint means a restart mid-run, a catch-up
    tick, or a second sweep cannot post a clan's card twice or DM the same
    person twice for one period.

    *Entitlement.* Every user gets exactly one unsolicited recap — their first —
    and opt-in is required for the rest. That rule needs no separate flag: "has
    this user ever been sent one" is a query against ``user_id`` here. Storing it
    per-delivery rather than as a boolean on the user also means we can see
    *which* card they got and when, which is what makes a support question
    answerable.

    ``subject_id`` is the card's subject (a group id, or the player id whose card
    was sent), while ``user_id`` is the human who received it. They differ on a
    DM: a user with several accounts is sent their best account's card, so the
    subject is that player and the recipient is the user.

    ``is_test`` marks a redirected send during rollout — those go to a test
    recipient rather than the real one, so they must not satisfy either question
    above. It is part of the unique key so a real send can still follow a test
    one for the same subject and period.
    """

    __tablename__ = "recap_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "scope", "subject_id", "period", "kind", "is_test",
            name="uq_recap_delivery_subject_period_kind",
        ),
        # "has this user ever had one" — the entitlement check, per user.
        Index("idx_recap_delivery_user", "user_id", "is_test"),
        # "what did this month's run do" — ops and reporting.
        Index("idx_recap_delivery_period", "period", "kind"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16), nullable=False)
    subject_id = Column(Integer, nullable=False)
    period = Column(String(7), nullable=False)
    kind = Column(String(16), nullable=False)
    # Null for a channel post. Set for a DM, and the column the entitlement
    # check reads.
    user_id = Column(Integer, nullable=True)
    # Discord snowflake of the channel or the recipient user. String, because
    # snowflakes exceed a signed 32-bit int and are ids, not numbers.
    target_id = Column(String(32), nullable=True)
    status = Column(String(16), nullable=False, default=DELIVERY_SENT)
    # Discord's id for the posted message, so a later edit or delete can find it.
    message_id = Column(String(32), nullable=True)
    error = Column(Text, nullable=True)
    is_test = Column(Integer, nullable=False, default=0, server_default="0")
    sent_at = Column(DateTime, nullable=False, server_default=func.now())
