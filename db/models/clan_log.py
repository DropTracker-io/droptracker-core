"""Clan Log — the catalog of obtainable uniques and a group's progress against it.

Three tables, two jobs.

**The catalog** (:class:`ClanLogSection` + :class:`ClanLogItem`) is the list of
*possible* slots: "these are the uniques Kree'arra drops". It has to be curated
rather than derived. ``xenforo.dt_npc_loot`` carries the wiki drop tables, but
its rarities are conditional on a unique roll for raids (Chambers of Xeric has
three lines under 1/50, none of them the uniques anyone means) and brand-new
content lands with no rows at all, so any rarity threshold both over- and
under-collects. The catalog is seeded from the curated loot-sweep sheet, checked
against the wiki tables, and thereafter edited by hand through ``/admin``.

**The progress ledger** (:class:`ClanLogFirst`) answers "did this clan get it,
and when". One row per ``(group, item, month)``: the *first* time anyone in the
group obtained that item in that month, plus how many times and how many
distinct members did. Every view folds out of that one shape —

* a month view is the rows for that month;
* a year view is the rows whose month falls in it, earliest row winning the
  attribution and the counts summing;
* the all-time view is every row, same rule.

Storing months rather than one row per (group, item, period) is what keeps the
three scopes consistent with each other and makes a new scope (a quarter, a
custom event window) a read change rather than a backfill.

``item_id`` throughout is the **OSRS item id**, not a catalog row id. Variants
(``variant_item_ids``) fold onto the canonical id when the ledger is written, so
editing, re-seeding or re-numbering the catalog never orphans a group's history.
"""
from __future__ import annotations

import json

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from .base import Base

# Bump when the snapshot payload shape changes in a way readers must notice.
CLAN_LOG_SCHEMA_VERSION = 1

# How a slot was credited. `drop` is the authority — a tracked drop naming the
# item. `clog` and `pet` are supplements for slots that never arrive as drops
# (untradeables the plugin reports as collection-log unlocks, pets, which come
# in as their own submission type); they can only ever ADD an obtained slot,
# never establish that one is missing.
SOURCE_DROP = "drop"
SOURCE_CLOG = "clog"
SOURCE_PET = "pet"
CLAN_LOG_SOURCES = (SOURCE_DROP, SOURCE_CLOG, SOURCE_PET)

# Snapshot scope value for `recap_snapshots`. Distinct from the recap scopes so
# the two features share the table without sharing rows.
SCOPE_CLAN_LOG = "clan_log"

# The all-time period token. 'YYYY' and 'YYYY-MM' are the other two, and all
# three fit `recap_snapshots.period` (String(7)).
PERIOD_ALL = "all"


class ClanLogSection(Base):
    """One boss / content area — the row of items a clan works through.

    ``npc_keys`` holds :func:`utils.npc_names.npc_match_key` outputs rather than
    display names, because that is the only comparison in this codebase that
    survives spelling, articles and the chest/collective encounters (every
    Barrows brother credits to ``Barrows``, the Moons to ``Lunar Chest``). A
    section can name several: "Vet'ion / Calvar'ion" is one board row.

    An empty ``npc_keys`` means "match on the item alone, from any source" — the
    honest encoding for the sheet's multi-source sets (Champion scrolls, the
    slayer miscellany), whose items are unique to those sources anyway.
    """

    __tablename__ = "clan_log_sections"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_clan_log_section_slug"),
        Index("idx_clan_log_section_order", "category", "sort_order"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Stable identity for re-seeding: the label is display text and may be
    # retitled, but the slug is what an upsert matches on.
    slug = Column(String(64), nullable=False)
    label = Column(String(96), nullable=False)
    category = Column(String(32), nullable=False, default="other")
    npc_keys = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def npc_key_list(self) -> list[str]:
        return _json_list(self.npc_keys)


class ClanLogItem(Base):
    """One obtainable slot within a section.

    ``attributable`` is False for slots we cannot prove *missing* — pets and
    clog-only untradeables arrive outside the drop pipeline, so their absence
    from ``drops`` means "not seen", not "not obtained". They still render (a
    clan wants its pets on the board), they are still credited when a ``pet`` or
    ``clog`` submission lands, and they are excluded from anything that treats
    the un-obtained set as a work list — the same carve-out
    ``services/loot_sweep`` makes when it skips pets.
    """

    __tablename__ = "clan_log_items"
    __table_args__ = (
        UniqueConstraint("section_id", "item_id", name="uq_clan_log_section_item"),
        Index("idx_clan_log_item_lookup", "item_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    section_id = Column(
        Integer, ForeignKey("clan_log_sections.id", ondelete="CASCADE"), nullable=False
    )
    # The canonical OSRS item id. Everything downstream keys on this.
    item_id = Column(Integer, nullable=False)
    item_name = Column(String(125), nullable=False)
    # Other ids that mean the same slot (charged/uncharged, damaged variants).
    variant_item_ids = Column(Text, nullable=True)
    attributable = Column(Boolean, nullable=False, default=True, server_default="1")
    source_hint = Column(String(8), nullable=False, default=SOURCE_DROP,
                         server_default=SOURCE_DROP)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def variant_ids(self) -> list[int]:
        return [int(v) for v in _json_list(self.variant_item_ids)]


class ClanLogFirst(Base):
    """A group's first claim on one item in one month.

    The unique key is the idempotency guard: the backfill and the incremental
    tail both upsert, so re-running either cannot double-count. ``obtained_at``
    only ever moves *earlier* on update — a later sighting of an item the group
    already had that month is a count, not a new first.
    """

    __tablename__ = "clan_log_firsts"
    __table_args__ = (
        UniqueConstraint("group_id", "item_id", "month", name="uq_clan_log_first"),
        # The read every view starts from: one group, one period window.
        Index("idx_clan_log_first_group_month", "group_id", "month"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=False)
    item_id = Column(Integer, nullable=False)
    month = Column(String(7), nullable=False)
    # Whoever got it first in this month. Not necessarily the only one — see
    # `player_count`, which is what lets the board say "+3 others".
    player_id = Column(Integer, nullable=False)
    obtained_at = Column(DateTime, nullable=False)
    drop_id = Column(Integer, nullable=True)
    source = Column(String(8), nullable=False, default=SOURCE_DROP,
                    server_default=SOURCE_DROP)
    # Screenshot for the first claim, when the submission carried one.
    proof_url = Column(String(500), nullable=True)
    obtained_count = Column(Integer, nullable=False, default=1, server_default="1")
    player_count = Column(Integer, nullable=False, default=1, server_default="1")
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


def _json_list(raw) -> list:
    """Tolerant JSON-list read. A malformed cell must not take a board down."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []
