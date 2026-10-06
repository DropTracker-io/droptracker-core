"""Admin widget routes: device pairing and the token-gated summary.

What these pin:

* only a superadmin session can pair, list or revoke a device;
* the raw token is returned once, at pairing, and only its SHA-256 is stored;
* the summary accepts nothing but a live ``dtw_`` bearer token whose owner is
  still a superadmin (a revoked token is 401, a demoted owner 403);
* a block that fails is null in the payload; the others still render;
* last_used_at is touched, but at most every few minutes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

import web_api.routes.admin_widget as aw

Base = declarative_base()


class AdminWidgetToken(Base):
    __tablename__ = "admin_widget_tokens"
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    user_id = sa.Column(sa.Integer, nullable=False)
    label = sa.Column(sa.String(64), nullable=False)
    token_hash = sa.Column(sa.String(64), nullable=False, unique=True)
    token_hint = sa.Column(sa.String(16), nullable=False)
    created_at = sa.Column(sa.DateTime, nullable=False, default=datetime.now)
    last_used_at = sa.Column(sa.DateTime)
    revoked_at = sa.Column(sa.DateTime)


OWNER, OTHER_ADMIN, NOBODY = 0, 7, 9


@pytest.fixture()
def env(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _db_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    admins = {OWNER, OTHER_ADMIN}
    audit = []
    state = SimpleNamespace(factory=factory, admins=admins, audit=audit, user=OWNER)

    monkeypatch.setattr(aw, "AdminWidgetToken", AdminWidgetToken)
    monkeypatch.setattr(aw, "db_session", _db_session)
    monkeypatch.setattr(aw, "load_user", lambda s, uid: SimpleNamespace(
        user_id=uid, is_superadmin=uid in state.admins))
    monkeypatch.setattr(aw, "current_user_id", lambda: state.user)
    monkeypatch.setattr(aw, "_audit", lambda *a, **k: audit.append(a))
    monkeypatch.setattr(aw, "support_stats", lambda s, uid: {"tickets_open": 3, "for": uid})
    monkeypatch.setattr(aw, "_cached_business", lambda s: {"mrr_cents": 12345})
    monkeypatch.setattr(aw, "dev_stats", lambda s: {"open_tasks": 4})
    return state


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


async def _pair(client, label="Pixel"):
    res = await client.post("/api/v1/admin/widget-tokens", json={"label": label})
    assert res.status_code == 201
    return await res.get_json()


def _rows(factory):
    with factory() as s:
        return s.query(AdminWidgetToken).order_by(AdminWidgetToken.id).all()


async def _summary(client, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.get("/api/v1/admin/widget/summary", headers=headers)


@pytest.mark.asyncio
async def test_pairing_returns_token_once_and_stores_only_its_hash(env, client):
    body = await _pair(client)
    raw = body["token"]
    assert raw.startswith("dtw_") and len(raw) > 40
    assert body["pair_url"] == f"droptracker-widget://pair?token={raw}"
    assert body["label"] == "Pixel"

    (row,) = _rows(env.factory)
    assert row.token_hash == aw.hash_token(raw)
    assert raw not in (row.token_hash, row.token_hint)
    assert row.token_hint == raw[:10]

    listed = await (await client.get("/api/v1/admin/widget-tokens")).get_json()
    assert [t["id"] for t in listed["tokens"]] == [body["id"]]
    assert "token" not in listed["tokens"][0]
    assert env.audit[0][1] == "admin_widget.pair"


@pytest.mark.asyncio
async def test_non_superadmin_cannot_pair_or_list(env, client):
    env.user = NOBODY
    res = await client.post("/api/v1/admin/widget-tokens", json={})
    assert res.status_code == 403
    assert (await client.get("/api/v1/admin/widget-tokens")).status_code == 403
    assert _rows(env.factory) == []


@pytest.mark.asyncio
async def test_summary_requires_a_live_token(env, client):
    raw = (await _pair(client))["token"]

    assert (await _summary(client, None)).status_code == 401
    assert (await _summary(client, "dtw_not-a-real-token")).status_code == 401
    # Session-shaped or other bearer values are not widget tokens.
    assert (await _summary(client, "eyJhbGciOi.jwt.value")).status_code == 401

    res = await _summary(client, raw)
    assert res.status_code == 200
    body = await res.get_json()
    assert body["user_id"] == OWNER
    assert body["support"] == {"tickets_open": 3, "for": OWNER}
    assert body["business"] == {"mrr_cents": 12345}
    assert body["dev"] == {"open_tasks": 4}
    assert "no-store" in res.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_revoked_token_is_rejected(env, client):
    body = await _pair(client)
    res = await client.delete(f"/api/v1/admin/widget-tokens/{body['id']}")
    assert res.status_code == 200
    assert (await res.get_json())["revoked_at"] is not None
    assert (await _summary(client, body["token"])).status_code == 401


@pytest.mark.asyncio
async def test_cannot_revoke_someone_elses_device(env, client):
    body = await _pair(client)
    env.user = OTHER_ADMIN
    res = await client.delete(f"/api/v1/admin/widget-tokens/{body['id']}")
    assert res.status_code == 404
    assert _rows(env.factory)[0].revoked_at is None


@pytest.mark.asyncio
async def test_demoted_owner_kills_the_token(env, client):
    raw = (await _pair(client))["token"]
    env.admins.discard(OWNER)
    assert (await _summary(client, raw)).status_code == 403


@pytest.mark.asyncio
async def test_failed_block_is_null_and_others_render(env, client, monkeypatch):
    raw = (await _pair(client))["token"]

    def boom(s):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(aw, "dev_stats", boom)
    body = await (await _summary(client, raw)).get_json()
    assert body["dev"] is None
    assert body["support"]["tickets_open"] == 3
    assert body["business"] == {"mrr_cents": 12345}


@pytest.mark.asyncio
async def test_last_used_is_touched_at_most_every_few_minutes(env, client):
    raw = (await _pair(client))["token"]
    await _summary(client, raw)
    first = _rows(env.factory)[0].last_used_at
    assert first is not None

    await _summary(client, raw)
    assert _rows(env.factory)[0].last_used_at == first

    with env.factory() as s:
        row = s.query(AdminWidgetToken).one()
        row.last_used_at = datetime.now() - timedelta(minutes=10)
        s.commit()
    await _summary(client, raw)
    assert _rows(env.factory)[0].last_used_at > first


@pytest.mark.asyncio
async def test_device_limit(env, client, monkeypatch):
    monkeypatch.setattr(aw, "_MAX_ACTIVE_TOKENS", 2)
    await _pair(client, "a")
    await _pair(client, "b")
    res = await client.post("/api/v1/admin/widget-tokens", json={"label": "c"})
    assert res.status_code == 409
