"""Known-issues board shown in the #status Discord channel and /admin/status.

Two-level structure: categories are the section headers, issues the entries
beneath them. Curated by superadmins in the web admin CP; the core bot renders
open issues into the status channel and re-renders when the web API bumps
status:issues:rev (services/status_metrics.py).
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import relationship

from .base import Base

# Rendering order + emoji for severities; validators import this tuple.
ISSUE_SEVERITIES = ("major", "degraded", "minor", "info")

ISSUE_STATUSES = ("open", "monitoring", "resolved")


class KnownIssueCategory(Base):
    __tablename__ = "known_issue_categories"
    __table_args__ = (
        Index("idx_known_issue_cat_order", "order", "id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    emoji = Column(String(32), nullable=True)
    order = Column(Integer, nullable=False, default=100)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    issues = relationship(
        "KnownIssue",
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="KnownIssue.order, KnownIssue.id",
    )


class KnownIssue(Base):
    __tablename__ = "known_issues"
    __table_args__ = (
        Index("idx_known_issue_category", "category_id", "order"),
        Index("idx_known_issue_status", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(
        Integer,
        ForeignKey("known_issue_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200), nullable=False)
    # Optional detail line rendered under the title (plain text, kept short).
    description = Column(Text, nullable=True)
    severity = Column(String(16), nullable=False, default="minor")
    status = Column(String(16), nullable=False, default="open")
    order = Column(Integer, nullable=False, default=100)
    # Display label of the superadmin who filed it (no FK — survives deletes).
    created_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    category = relationship("KnownIssueCategory", back_populates="issues")
