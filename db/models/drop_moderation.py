"""Per-(drop, group) moderation state for manual submissions (suggestion #45).

Absence of a row = the drop counts normally for that group. Rows exist only
when a group's ``manual_submission_policy`` withheld a manual drop from that
group's boards/notifications:

- ``excluded``  — policy 'block', or 'authorized_only' with an unauthorized
                  submitter. Permanent (Phase 1).
- ``pending``   — policy 'confirm': awaiting a group admin's review (Phase 2).
- ``approved``  — admin approved a pending row; the drop counts for the group
                  (kept for audit; readers must NOT exclude it).
- ``rejected``  — admin rejected a pending row.

The drop row itself is always written and always counts globally and for
groups without a restrictive policy — one group's policy never affects
another group or global tracking.

Every group-scoped aggregation over the ``drops`` table must exclude drops
with a row in EXCLUDING_STATUSES for that group (intake Redis increments are
filtered at write time; lootboard generation, the reconcile_* scripts and the
force-update rebuild apply the same filter — see
``services/drop_moderation.py``).
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy import func

from .base import Base

# Statuses that keep a drop OFF the group's boards/notifications.
EXCLUDING_STATUSES = ("excluded", "pending", "rejected")


class DropGroupModeration(Base):
    __tablename__ = "drop_group_moderation"
    __table_args__ = (
        Index("uq_drop_group_moderation", "drop_id", "group_id", unique=True),
        Index("idx_drop_moderation_group_status", "group_id", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    drop_id = Column(Integer, ForeignKey("drops.drop_id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    status = Column(String(16), nullable=False, default="excluded")
    # Why the row exists, e.g. "policy:block" / "policy:authorized_only".
    reason = Column(String(64), nullable=True)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
