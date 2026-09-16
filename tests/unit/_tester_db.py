"""A miniature, real database for the tester-roster and badge-group tests.

The unit suite stubs ``db.models`` (tests/conftest.py), and the code under test
imports its models lazily from there. These tables mirror the real columns and
unique keys the code relies on, on in-memory SQLite, so upserts, savepoints and
unique-key clashes behave the way MariaDB's do.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import declarative_base, sessionmaker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    user_id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    discord_id = sa.Column(sa.String(35), unique=True)
    auth_token = sa.Column(sa.String(16), nullable=False)
    username = sa.Column(sa.String(20))


class Player(Base):
    __tablename__ = "players"
    player_id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    wom_id = sa.Column(sa.Integer, unique=True)
    account_hash = sa.Column(sa.String(100), unique=True, nullable=True)
    player_name = sa.Column(sa.String(20))
    user_id = sa.Column(sa.Integer, sa.ForeignKey("users.user_id"))
    log_slots = sa.Column(sa.Integer)
    total_level = sa.Column(sa.Integer)
    account_type = sa.Column(sa.String(32))


class Badge(Base):
    __tablename__ = "badges"
    badge_id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    key = sa.Column(sa.String(64), nullable=False, unique=True)
    name = sa.Column(sa.String(100), nullable=False)
    description = sa.Column(sa.String(300), nullable=False)
    icon_url = sa.Column(sa.String(512))
    icon_emoji = sa.Column(sa.String(16))
    tone = sa.Column(sa.String(16), nullable=False, default="gold")
    semantic = sa.Column(sa.String(16), nullable=False, default="permanent")
    scope = sa.Column(sa.String(16), nullable=False, default="global")
    active = sa.Column(sa.Boolean, nullable=False, default=True)
    criteria = sa.Column(sa.Text)


class PlayerBadge(Base):
    __tablename__ = "player_badges"
    __table_args__ = (
        sa.Index("ux_player_badges_active", "badge_id", "group_key", "active_key", unique=True),
    )
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    badge_id = sa.Column(sa.Integer, sa.ForeignKey("badges.badge_id"), nullable=False)
    player_id = sa.Column(sa.Integer, sa.ForeignKey("players.player_id"), nullable=False)
    group_id = sa.Column(sa.Integer, nullable=True)
    group_key = sa.Column(sa.Integer, nullable=False, default=0)
    status = sa.Column(sa.String(16), nullable=False, default="active")
    slot_key = sa.Column(sa.String(64), nullable=False, default="")
    active_key = sa.Column(sa.String(64), nullable=True)
    awarded_at = sa.Column(sa.DateTime, nullable=False, default=datetime.now)
    lost_at = sa.Column(sa.DateTime)
    awarded_by = sa.Column(sa.Integer)
    context = sa.Column(sa.Text)


class Group(Base):
    __tablename__ = "groups"
    group_id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    group_name = sa.Column(sa.String(30))
    wom_id = sa.Column(sa.Integer)
    guild_id = sa.Column(sa.String(255))


user_group_association = sa.Table(
    "user_group_association", Base.metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("player_id", sa.Integer, nullable=True),
    sa.Column("user_id", sa.Integer, nullable=True),
    sa.Column("group_id", sa.Integer, nullable=False),
)


def make_sessionmaker():
    """A fresh in-memory database, with SAVEPOINTs that actually work on pysqlite."""
    engine = sa.create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _no_implicit_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def install_models(monkeypatch, session_factory) -> None:
    """Point the stubbed ``db.models`` at these tables for one test."""
    models = sys.modules["db.models"]
    for name, value in (("User", User), ("Player", Player), ("Badge", Badge),
                        ("PlayerBadge", PlayerBadge), ("Group", Group),
                        ("user_group_association", user_group_association),
                        ("Session", session_factory)):
        monkeypatch.setattr(models, name, value, raising=False)


def load(module_name: str, *path_parts: str, register_as: str = None, monkeypatch=None):
    """Load a real module from the repo by path (its package is stubbed)."""
    path = os.path.join(REPO_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if register_as and monkeypatch is not None:
        monkeypatch.setitem(sys.modules, register_as, module)
    return module


def add_badge(session, key="bug_tester_helper", active=True) -> Badge:
    badge = Badge(key=key, name="Bug Tester", description="Helps test DropTracker",
                  tone="green", semantic="permanent", scope="global", active=active)
    session.add(badge)
    session.flush()
    return badge


def award(session, badge, player_id, status="active") -> PlayerBadge:
    slot = f"p:{player_id}"
    row = PlayerBadge(badge_id=badge.badge_id, player_id=player_id, group_key=0,
                      status=status, slot_key=slot,
                      active_key=slot if status == "active" else None)
    session.add(row)
    session.flush()
    return row
