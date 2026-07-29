"""Recap snapshot ORM model (monthly / annual "Wrapped").

One row per (scope, subject, period) holding a fully-computed recap card as
JSON. The snapshot *is* the artifact — pages, images and Discord posts all read
it rather than recomputing, which is what makes a permanent public URL cheap.

Why persisted and not cached:

* the annual recap is a fold over twelve monthly rows, so those rows must
  outlive any TTL;
* ``/groups/{id}/recap/{period}`` is meant to stay valid forever;
* the biggest-drop card points at a screenshot that
  ``droptracker-prune-images.timer`` deletes once it is 30 days old and worth
  under 1M GP — freezing the payload captures the URL while the file exists.

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
