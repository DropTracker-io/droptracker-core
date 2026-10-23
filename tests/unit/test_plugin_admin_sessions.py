"""Plugin group-admin device sessions (db/plugin_admin_sessions.py, web143a).

What these pin:

* the token is returned once and only its hash is stored;
* a session verifies only for its own group and account hash;
* resetting or clearing the group's code (a new generation, or no code)
  revokes every session minted before, with no sweep;
* linking again replaces the account's previous session;
* an idle session expires and is revoked when found; use slides the window;
* unlink revokes; the Authorization header must be a well-formed bearer token.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_real_pas", _ROOT / "db" / "plugin_admin_sessions.py")
pas = importlib.util.module_from_spec(_spec)
sys.modules["_real_pas"] = pas
_spec.loader.exec_module(pas)

Base = declarative_base()


class GroupVerificationCode(Base):
    __tablename__ = "group_verification_codes"
    group_id = sa.Column(sa.Integer, primary_key=True)
    code_hash = sa.Column(sa.String(80))
    generation = sa.Column(sa.Integer, nullable=False, default=0)


class PluginAdminSession(Base):
    __tablename__ = "plugin_admin_sessions"
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    token_hash = sa.Column(sa.String(64), nullable=False, unique=True)
    group_id = sa.Column(sa.Integer, nullable=False)
    user_id = sa.Column(sa.Integer, nullable=False)
    account_hash = sa.Column(sa.String(100), nullable=False)
    generation = sa.Column(sa.Integer, nullable=False)
    created_at = sa.Column(sa.DateTime, nullable=False)
    last_used_at = sa.Column(sa.DateTime)
    revoked_at = sa.Column(sa.DateTime)
    revoked_reason = sa.Column(sa.String(16))


NOW = datetime(2026, 10, 22, 12, 0, 0)


@pytest.fixture()
def s():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    pas.PluginAdminSession, pas.GroupVerificationCode = PluginAdminSession, GroupVerificationCode
    session = sessionmaker(bind=engine)()
    session.add(GroupVerificationCode(group_id=7, code_hash="v1:jwt:abc", generation=3))
    session.add(GroupVerificationCode(group_id=8, code_hash="v1:jwt:def", generation=1))
    session.commit()
    yield session
    session.close()
    pas.PluginAdminSession = pas.GroupVerificationCode = None


def mint(s, group_id=7, account="111", user_id=0, now=NOW):
    return pas.mint(s, group_id, user_id, account, pas.current_generation(s, group_id), now=now)


def test_the_token_is_returned_once_and_only_hashed(s):
    token = mint(s)
    assert token.startswith("dtp_") and len(token) > 40
    row = s.query(PluginAdminSession).one()
    assert row.token_hash == pas.hash_token(token) and token not in row.token_hash
    # user_id 0 is a real account.
    assert row.user_id == 0 and row.generation == 3


def test_a_session_verifies_for_its_own_group_and_account_only(s):
    token = mint(s)
    assert pas.verify(s, token, 7, "111", now=NOW)[1] == "ok"
    assert pas.verify(s, token, 8, "111", now=NOW) == (None, "group")
    assert pas.verify(s, token, 7, "222", now=NOW) == (None, "account")
    assert pas.verify(s, "dtp_nope", 7, "111", now=NOW) == (None, "unknown")
    assert pas.verify(s, None, 7, "111", now=NOW) == (None, "missing")


def test_resetting_or_clearing_the_code_revokes_every_session(s):
    token = mint(s)
    other = mint(s, account="222")
    s.query(GroupVerificationCode).filter_by(group_id=7).update({"generation": 4})
    assert pas.verify(s, token, 7, "111", now=NOW) == (None, "code")
    assert pas.verify(s, other, 7, "222", now=NOW) == (None, "code")
    fresh = mint(s)
    assert pas.verify(s, fresh, 7, "111", now=NOW)[1] == "ok"
    s.query(GroupVerificationCode).filter_by(group_id=7).update({"code_hash": None})
    assert pas.verify(s, fresh, 7, "111", now=NOW) == (None, "code")


def test_linking_again_replaces_the_previous_session(s):
    first = mint(s)
    second = mint(s)
    assert pas.verify(s, first, 7, "111", now=NOW) == (None, "revoked")
    assert pas.verify(s, second, 7, "111", now=NOW)[1] == "ok"
    # Another group's session for the same account is left alone.
    elsewhere = mint(s, group_id=8)
    assert pas.verify(s, second, 7, "111", now=NOW)[1] == "ok"
    assert pas.verify(s, elsewhere, 8, "111", now=NOW)[1] == "ok"


def test_idle_sessions_expire_and_use_slides_the_window(s):
    token = mint(s)
    later = NOW + timedelta(days=pas.IDLE_DAYS - 1)
    assert pas.verify(s, token, 7, "111", now=later)[1] == "ok"
    assert pas.verify(s, token, 7, "111", now=later + timedelta(days=pas.IDLE_DAYS - 1))[1] == "ok"
    gone = later + timedelta(days=2 * pas.IDLE_DAYS)
    assert pas.verify(s, token, 7, "111", now=gone) == (None, "expired")
    assert s.query(PluginAdminSession).one().revoked_reason == "expired"


def test_unlink_revokes(s):
    token = mint(s)
    row, _ = pas.verify(s, token, 7, "111", now=NOW)
    pas.revoke(s, row, "unlink", now=NOW)
    assert pas.verify(s, token, 7, "111", now=NOW) == (None, "revoked")


def test_linked_group_ids_lists_only_sessions_that_would_verify(s):
    mint(s, group_id=7)
    mint(s, group_id=8)
    assert pas.linked_group_ids(s, "111", 0, now=NOW) == {7, 8}
    s.query(GroupVerificationCode).filter_by(group_id=8).update({"generation": 2})
    assert pas.linked_group_ids(s, "111", 0, now=NOW) == {7}
    assert pas.linked_group_ids(s, "111", 5, now=NOW) == set()


@pytest.mark.parametrize("header,token", [
    ("Bearer dtp_abc", "dtp_abc"), ("bearer  dtp_abc ", "dtp_abc"),
    (None, None), ("", None), ("Basic dtp_abc", None), ("Bearer abc", None), ("Bearer dtp_" + "x" * 200, None),
])
def test_bearer_header(header, token):
    assert pas.bearer(header) == token
