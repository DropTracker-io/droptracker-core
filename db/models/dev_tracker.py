"""Internal dev-tracker models (the owner's project/task/note board).

A lightweight in-house Trello for tracking features: what is planned, what is
in flight, what shipped, plus free-form notes on ideas and suggestions. It is
strictly admin-CP-facing — nothing here is exposed on any public surface.

Hierarchy: ``DevProject`` → ``DevTask`` → ``DevSubtask``, with ``DevNote``
rows attachable to a project or to a specific task within it. Completion is
tracked independently at every level (checking every subtask does NOT
auto-complete the task) and each completable thing carries an optional
completion note.

Two write surfaces share these models:
  * ``web_api/routes/dev_tracker.py`` — superadmin CMS at ``/admin/projects``
    on the site (same DB-table-as-CMS pattern as ``DocsPage``/``SiteRedirect``).
  * ``scripts/project_tracker.py`` — CLI for codebase agents on this box, who
    have DB access but no Discord-OAuth session.

``author`` is a free-text label rather than a ``users`` FK because CLI
writers (agents) have no site account.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from .base import Base

# Canonical status vocabularies. Route validation and the CLI both import
# these; keep them tuples (ordered — the UI renders selects in this order).
PROJECT_STATUSES = ("active", "completed", "archived")
TASK_STATUSES = ("planned", "in_progress", "blocked", "done")


class DevProject(Base):
    """A tracked feature/initiative; the top of the tree."""

    __tablename__ = "dev_projects"
    __table_args__ = (
        Index("idx_dev_project_status_order", "status", "order"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="active")
    completion_note = Column(Text, nullable=True)
    order = Column(Integer, nullable=False, default=100)
    author = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    # Touched manually by both write surfaces on child (task/subtask/note)
    # mutations too, so it doubles as "last activity".
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    tasks = relationship(
        "DevTask",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="DevTask.order, DevTask.id",
    )
    notes = relationship(
        "DevNote",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="DevNote.id",
    )


class DevTask(Base):
    """A unit of work inside a project; holds the plan text (``body_md``)."""

    __tablename__ = "dev_tasks"
    __table_args__ = (
        Index("idx_dev_task_project", "project_id", "order"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(
        Integer, ForeignKey("dev_projects.id", ondelete="CASCADE"), nullable=False
    )
    title = Column(String(300), nullable=False)
    body_md = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="planned")
    completion_note = Column(Text, nullable=True)
    order = Column(Integer, nullable=False, default=100)
    author = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    project = relationship("DevProject", back_populates="tasks")
    subtasks = relationship(
        "DevSubtask",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="DevSubtask.order, DevSubtask.id",
    )
    notes = relationship(
        "DevNote",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="DevNote.id",
    )


class DevSubtask(Base):
    """A checkable line item inside a task ("individual portion")."""

    __tablename__ = "dev_subtasks"
    __table_args__ = (
        Index("idx_dev_subtask_task", "task_id", "order"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=False
    )
    title = Column(String(300), nullable=False)
    done = Column(Boolean, nullable=False, default=False)
    note = Column(String(500), nullable=True)
    order = Column(Integer, nullable=False, default=100)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    task = relationship("DevTask", back_populates="subtasks")


class DevNote(Base):
    """Free-form Markdown note on a project or (when ``task_id`` is set) on a
    specific task within it. ``project_id`` is always set — it keeps
    project-level listing/deletion trivial and is validated to match the
    task's project on the write paths."""

    __tablename__ = "dev_notes"
    __table_args__ = (
        Index("idx_dev_note_project", "project_id"),
        Index("idx_dev_note_task", "task_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(
        Integer, ForeignKey("dev_projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id = Column(
        Integer, ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=True
    )
    body_md = Column(Text, nullable=False)
    author = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("DevProject", back_populates="notes")
    task = relationship("DevTask", back_populates="notes")
