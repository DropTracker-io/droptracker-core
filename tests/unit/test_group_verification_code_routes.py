"""Plugin verification code routes (web141a).

What these pin:

* only the group's actual owner can reveal or reset; an admin cannot, and
  neither can a superadmin acting through the owner routes;
* an ownerless group is told to claim ownership first;
* reveal works once; after that the code can only be replaced by a reset,
  which invalidates the old code and is announced in the group's Discord;
* the code appears only in the reveal/reset response, never in the roster
  payload, and responses are not cacheable;
* staff can see whether a code is active and clear it, but never see it, and
  the owner can then reveal a fresh one.
"""
from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

import web_api.deps as deps
import web_api.routes.admin as admin
import web_api.routes.group_admin as ga

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_real_gvc_routes", _ROOT / "db" / "group_verification_codes.py")
gvc = importlib.util.module_from_spec(_spec)
sys.modules["_real_gvc_routes"] = gvc
_spec.loader.exec_module(gvc)

Base = declarative_base()


class GroupVerificationCode(Base):
    __tablename__ = "group_verification_codes"
    group_id = sa.Column(sa.Integer, primary_key=True)
    code_hash = sa.Column(sa.String(80))
    generation = sa.Column(sa.Integer, nullable=False, default=0)
    issued_via = sa.Column(sa.String(16))
    issued_at = sa.Column(sa.DateTime)
    issued_by_user_id = sa.Column(sa.Integer)
    cleared_at = sa.Column(sa.DateTime)
    cleared_by_user_id = sa.Column(sa.Integer)
    cleared_reason = sa.Column(sa.String(32))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    actor_user_id = sa.Column(sa.Integer)
    group_id = sa.Column(sa.Integer)
    action = sa.Column(sa.String(64))
    target = sa.Column(sa.String(128))
    before = sa.Column(sa.Text)
    after = sa.Column(sa.Text)


class Group(Base):
    __tablename__ = "groups"
    group_id = sa.Column(sa.Integer, primary_key=True)


GID = 5
# user_id 0 is a real account; the owner being 0 keeps truthiness bugs honest.
OWNER, ADMIN, STAFF = 0, 7, 99


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JWT_TOKEN_KEY", "jwt-secret-for-tests")
    monkeypatch.delenv("GROUP_CODE_PEPPER", raising=False)
    monkeypatch.setattr(gvc, "GroupVerificationCode", GroupVerificationCode)
    monkeypatch.setattr(sys.modules["db"], "group_verification_codes", gvc, raising=False)

    engine = sa.create_engine("sqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add(Group(group_id=GID))
        s.commit()

    @contextmanager
    def _db_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    state = SimpleNamespace(factory=factory, user=OWNER, owner=OWNER, notices=[])

    for mod in (ga, admin):
        monkeypatch.setattr(mod, "db_session", _db_session)
        monkeypatch.setattr(mod, "AuditLog", AuditLog)
    monkeypatch.setattr(admin, "Group", Group)

    monkeypatch.setattr(ga, "current_user_id", lambda: state.user)
    monkeypatch.setattr(ga, "load_user", lambda s, uid: SimpleNamespace(
        user_id=uid, username=f"user{uid}", is_superadmin=uid == STAFF))
    owner_of = lambda s, gid: state.owner  # noqa: E731
    monkeypatch.setattr(ga, "group_owner_user_id", owner_of)
    monkeypatch.setattr(deps, "group_owner_user_id", owner_of)
    monkeypatch.setattr(ga, "is_group_owner", lambda s, uid, gid, user=None: uid in (state.owner, STAFF))
    monkeypatch.setattr(ga, "assert_group_admin", lambda *a, **k: "admin")
    monkeypatch.setattr(ga, "manageable_guild_ids", lambda uid: set())
    monkeypatch.setattr(ga, "_authorized_entries", lambda s, gid: [])
    monkeypatch.setattr(ga, "discord_perms_grant_admin", lambda s, gid: True)
    monkeypatch.setattr(ga, "_notify_group", lambda s, gid, content: state.notices.append(content))

    async def _staff():
        return STAFF

    monkeypatch.setattr(admin, "_require_superadmin", _staff)
    return state


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _digits(code):
    return code.replace("-", "")


def _row(env):
    with env.factory() as s:
        return s.query(GroupVerificationCode).filter_by(group_id=GID).first()


def _actions(env):
    with env.factory() as s:
        return [a.action for a in s.query(AuditLog).order_by(AuditLog.id)]


async def _reveal(client):
    return await client.post(f"/api/v1/groups/{GID}/verification-code/reveal")


async def _reset(client):
    return await client.post(f"/api/v1/groups/{GID}/verification-code/reset")


@pytest.mark.asyncio
async def test_owner_reveals_once(env, client):
    resp = await _reveal(client)
    assert resp.status_code == 200
    body = await resp.get_json()
    assert len(body["code"]) == 11
    assert body["verification_code"]["active"] is True
    assert body["verification_code"]["can_manage"] is True
    assert "no-store" in resp.headers.get("Cache-Control", "")
    assert gvc.matches(_row(env).code_hash, GID, _digits(body["code"]))
    assert _actions(env) == ["group.verification_code.reveal"]
    assert env.notices == []

    again = await _reveal(client)
    assert again.status_code == 409
    assert (await again.get_json())["code"] == "code_already_revealed"
    assert _row(env).generation == 1


@pytest.mark.asyncio
async def test_roster_shows_status_never_the_code(env, client):
    code = (await (await _reveal(client)).get_json())["code"]
    resp = await client.get(f"/api/v1/groups/{GID}/authorized-users")
    body = await resp.get_json()
    assert body["verification_code"]["active"] is True
    assert "code" not in body
    assert _digits(code) not in await resp.get_data(as_text=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [ADMIN, STAFF])
async def test_only_the_real_owner_may_reveal(env, client, who):
    env.user = who
    resp = await _reveal(client)
    assert resp.status_code == 403
    assert _row(env) is None


@pytest.mark.asyncio
async def test_admins_see_status_without_manage_rights(env, client):
    env.user = ADMIN
    body = await (await client.get(f"/api/v1/groups/{GID}/authorized-users")).get_json()
    assert body["verification_code"]["can_manage"] is False


@pytest.mark.asyncio
async def test_ownerless_group_must_claim_first(env, client):
    env.owner = None
    resp = await _reveal(client)
    assert resp.status_code == 409
    assert (await resp.get_json())["code"] == "group_ownerless"


@pytest.mark.asyncio
async def test_reset_replaces_the_code_and_announces_it(env, client):
    assert (await _reset(client)).status_code == 409  # nothing to reset yet

    first = (await (await _reveal(client)).get_json())["code"]
    resp = await _reset(client)
    assert resp.status_code == 200
    second = (await resp.get_json())["code"]
    row = _row(env)
    assert row.generation == 2 and row.issued_via == "reset"
    assert gvc.matches(row.code_hash, GID, _digits(second))
    if first != second:
        assert not gvc.matches(row.code_hash, GID, _digits(first))
    assert len(env.notices) == 1 and "reset" in env.notices[0]
    assert _actions(env) == ["group.verification_code.reveal", "group.verification_code.reset"]


@pytest.mark.asyncio
async def test_admin_cannot_reset(env, client):
    await _reveal(client)
    env.user = ADMIN
    assert (await _reset(client)).status_code == 403
    assert _row(env).generation == 1


@pytest.mark.asyncio
async def test_missing_key_is_503_not_a_weak_hash(env, client, monkeypatch):
    for name in ("GROUP_CODE_PEPPER", "JWT_TOKEN_KEY", "ENCRYPTION_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert (await _reveal(client)).status_code == 503
    assert _row(env) is None


@pytest.mark.asyncio
async def test_staff_clear_then_owner_reveals_again(env, client):
    first = (await (await _reveal(client)).get_json())["code"]

    status = await (await client.get(f"/api/v1/admin/groups/{GID}/verification-code")).get_json()
    assert status["active"] is True and status["owner_user_id"] == OWNER
    assert "code" not in status

    resp = await client.post(f"/api/v1/admin/groups/{GID}/verification-code/clear",
                             json={"note": "ticket 123"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["active"] is False and body["cleared_reason"] == "staff"
    assert _digits(first) not in await resp.get_data(as_text=True)
    assert _row(env).code_hash is None
    assert any("staff cleared" in n for n in env.notices)

    again = await client.post(f"/api/v1/admin/groups/{GID}/verification-code/clear", json={})
    assert again.status_code == 409

    env.user = OWNER
    fresh = await _reveal(client)
    assert fresh.status_code == 200
    assert _row(env).generation == 3
    assert _actions(env) == [
        "group.verification_code.reveal",
        "group.verification_code.clear",
        "group.verification_code.reveal",
    ]


@pytest.mark.asyncio
async def test_staff_routes_404_unknown_group(env, client):
    resp = await client.get("/api/v1/admin/groups/404/verification-code")
    assert resp.status_code == 404
