"""Event clan-point award routes (web114a) — auth, validation and the
award/revoke guards, driven through the app with the scripted-session harness.

The service module is swapped for a namespace built from the REAL pure core
(loaded by file path) plus recording fakes for its DB-touching half, so these
pin what the routes decide — who may write, what is refused and why — without
re-testing the scoring (tests/unit/test_event_point_awards.py does that).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

import web_api.routes.event_points as evp
from tests.unit.test_event_auth_modes import _S, _SessionCM

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_point_awards.py",
)
_spec = importlib.util.spec_from_file_location("_event_point_awards_routes_ut", _MODULE_PATH)
epa = importlib.util.module_from_spec(_spec)
sys.modules["_event_point_awards_routes_ut"] = epa
_spec.loader.exec_module(epa)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


class _RecConfig:
    """Recording stand-in for the ORM row (class attrs double as the filter
    columns the scripted _Q ignores)."""

    event_id = group_id = status = None

    def __init__(self, **kw):
        self.config = None
        self.awarded_at = None
        self.last_error = None
        self.__dict__.update(kw)


class _RecAudit:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _event(**kw):
    base = dict(id=1, group_id=14, name="Bingo", status="past", kind="bingo",
                mode="standard", visibility="public", effort_visibility="public")
    base.update(kw)
    return SimpleNamespace(**base)


def _svc(*, row=None, active=True, plan=None, calls=None):
    calls = calls if calls is not None else []
    ns = SimpleNamespace(
        PointsConfigError=epa.PointsConfigError,
        effective_points_config=epa.effective_points_config,
        normalize_points_input=epa.normalize_points_input,
        merge_points_config=epa.merge_points_config,
        pays_anything=epa.pays_anything,
        needs_ehe_pricing=epa.needs_ehe_pricing,
        summarize=epa.summarize,
    )
    ns.scope_group_ids = lambda s, ev: [ev.group_id] if ev.group_id else []
    ns.points_system_active = lambda gid: active
    ns.load_config_row = lambda s, eid, gid, for_update=False: row
    ns.compute_plan = lambda s, ev, gid, cfg: plan or {
        "rows": [], "skipped": [], "placements": [], "competition": False,
        "ehe_supported": True, "rates_known": True, "roster_size": 0}

    def _blocker(s, ev, cfg_row, plan=None):
        # The real rule, with the fakes' notion of the points system.
        epa.points_system_active = lambda gid: active
        return epa.award_blocker(s, ev, cfg_row, plan)

    ns.award_blocker = _blocker

    def _apply(s, ev, cfg_row, *, actor_user_id=None, plan=None):
        calls.append(("apply", cfg_row.group_id, actor_user_id))
        cfg_row.status = "awarded"
        return {"status": "awarded", "inserted": 3, "placements": []}

    def _revoke(s, ev, cfg_row, *, actor_user_id=None):
        calls.append(("revoke", cfg_row.group_id, actor_user_id))
        cfg_row.status = "revoked"
        return {"status": "revoked", "removed": 3, "removed_points": 300}

    ns.apply_award = _apply
    ns.revoke_awards = _revoke
    ns.publish_update = lambda *a, **k: calls.append(("publish",))
    return ns


def _wire(monkeypatch, session, svc, *, ev=None, manage=True, superadmin=False):
    ev = ev or _event()
    monkeypatch.setattr(evp, "current_user_id", lambda: 7)
    monkeypatch.setattr(evp, "optional_user_id", lambda: 7)
    monkeypatch.setattr(evp, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evp, "_svc", lambda: svc)
    monkeypatch.setattr(evp, "_load_event_or_404", lambda s, eid: ev)
    monkeypatch.setattr(evp, "load_user",
                        lambda s, uid: SimpleNamespace(user_id=uid, is_superadmin=superadmin))
    monkeypatch.setattr(evp, "is_superadmin", lambda u: bool(u and u.is_superadmin))
    monkeypatch.setattr(evp, "_can_manage", lambda s, uid, user, gid: manage)
    monkeypatch.setattr(evp, "_payload",
                        lambda s, e, viewer, only_group=None, preview=False:
                        {"event_id": e.id, "scopes": [], "only_group": only_group})
    monkeypatch.setattr(evp, "EventPointConfig", _RecConfig)
    monkeypatch.setattr(evp, "AuditLog", _RecAudit)
    return ev


# --------------------------------------------------------------------------- #
# PUT /events/{id}/clan-points
# --------------------------------------------------------------------------- #
class TestPut:
    async def test_creates_row_with_merged_config_and_audits(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, _svc())
        r = await client.put("/api/v1/events/1/clan-points", json={
            "config": {"enabled": True, "placement": [100, 50]}})
        assert r.status_code == 200
        assert (await r.get_json())["only_group"] == 14
        row = next(a for a in s.added if isinstance(a, _RecConfig))
        assert (row.event_id, row.group_id, row.status) == (1, 14, "pending")
        stored = json.loads(row.config)
        assert stored["enabled"] is True and stored["placement"] == [100, 50]
        assert stored["participation"] == {"flat": 0, "per_hour": 0, "min_hours": 0, "max": 0}
        audit = next(a for a in s.added if isinstance(a, _RecAudit))
        assert audit.action == "event.points.config" and audit.event_id == 1
        assert s.committed

    async def test_non_manager_forbidden(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(), manage=False)
        r = await client.put("/api/v1/events/1/clan-points", json={"config": {"enabled": True}})
        assert r.status_code == 403
        assert (await r.get_json())["code"] == "clan_points_manager_required"

    async def test_invalid_config_422_with_reason(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc())
        r = await client.put("/api/v1/events/1/clan-points",
                             json={"config": {"placement": [100, -5]}})
        assert r.status_code == 422
        assert "placement" in (await r.get_json())["detail"]

    async def test_enabling_needs_the_points_system(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(active=False))
        r = await client.put("/api/v1/events/1/clan-points", json={"config": {"enabled": True}})
        assert r.status_code == 403
        assert (await r.get_json())["entitlement"] == "custom_points"

    async def test_superadmin_may_configure_without_it(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, _svc(active=False), superadmin=True)
        r = await client.put("/api/v1/events/1/clan-points", json={"config": {"enabled": True}})
        assert r.status_code == 200

    async def test_disabled_config_saves_without_the_points_system(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s, _svc(active=False))
        r = await client.put("/api/v1/events/1/clan-points",
                             json={"config": {"placement": [5]}})
        assert r.status_code == 200

    async def test_switching_a_deferred_payout_to_review_parks_it(self, client, monkeypatch):
        row = _RecConfig(event_id=1, group_id=14, status="deferred",
                         config=json.dumps({"enabled": True, "award_mode": "auto"}))
        s = _S()
        _wire(monkeypatch, s, _svc(row=row))
        r = await client.put("/api/v1/events/1/clan-points",
                             json={"config": {"award_mode": "review"}})
        assert r.status_code == 200
        assert row.status == "pending"

    async def test_no_op_save_writes_no_audit(self, client, monkeypatch):
        row = _RecConfig(event_id=1, group_id=14, status="pending",
                         config=json.dumps({"enabled": True, "placement": [10]}))
        s = _S()
        _wire(monkeypatch, s, _svc(row=row))
        r = await client.put("/api/v1/events/1/clan-points",
                             json={"config": {"placement": [10]}})
        assert r.status_code == 200
        assert not any(isinstance(a, _RecAudit) for a in s.added)

    async def test_global_event_has_no_clan(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(), ev=_event(group_id=None))
        r = await client.put("/api/v1/events/1/clan-points", json={"config": {}})
        assert r.status_code == 422
        assert (await r.get_json())["code"] == "clan_points_no_clan"

    async def test_foreign_clan_404(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc())
        r = await client.put("/api/v1/events/1/clan-points",
                             json={"group_id": 99, "config": {}})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# POST award / revoke
# --------------------------------------------------------------------------- #
def _row(**kw):
    base = dict(event_id=1, group_id=14, status="pending",
                config=json.dumps({"enabled": True, "placement": [100]}))
    base.update(kw)
    return _RecConfig(**base)


class TestAward:
    async def test_awards_and_publishes(self, client, monkeypatch):
        calls = []
        s = _S()
        _wire(monkeypatch, s, _svc(row=_row(), calls=calls))
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["summary"] == {"status": "awarded", "inserted": 3}
        assert calls == [("apply", 14, 7), ("publish",)]
        assert s.committed

    async def test_refused_while_the_event_runs(self, client, monkeypatch):
        calls = []
        _wire(monkeypatch, _S(), _svc(row=_row(), calls=calls), ev=_event(status="active"))
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 409
        body = await r.get_json()
        assert body["code"] == "clan_points_blocked" and "ended" in body["detail"]
        assert calls == []

    async def test_refused_without_the_points_system(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(row=_row(), active=False))
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 409
        assert "points system" in (await r.get_json())["detail"]

    async def test_refused_on_cold_ehe_rates(self, client, monkeypatch):
        row = _row(config=json.dumps({"enabled": True, "participation": {"per_hour": 2}}))
        plan = {"rows": [], "skipped": [], "placements": [], "competition": False,
                "ehe_supported": True, "rates_known": False, "roster_size": 3}
        _wire(monkeypatch, _S(), _svc(row=row, plan=plan))
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 409
        assert "EHE" in (await r.get_json())["detail"]

    async def test_unconfigured_409(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(row=None))
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 409
        assert (await r.get_json())["code"] == "clan_points_unconfigured"

    async def test_non_manager_forbidden(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(row=_row()), manage=False)
        r = await client.post("/api/v1/events/1/clan-points/award", json={})
        assert r.status_code == 403


class TestRevoke:
    async def test_requires_confirmation(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(row=_row(status="awarded")))
        r = await client.post("/api/v1/events/1/clan-points/revoke", json={})
        assert r.status_code == 400

    async def test_revokes(self, client, monkeypatch):
        calls = []
        row = _row(status="awarded")
        _wire(monkeypatch, _S(), _svc(row=row, calls=calls))
        r = await client.post("/api/v1/events/1/clan-points/revoke", json={"confirm": "revoke"})
        assert r.status_code == 200
        assert (await r.get_json())["summary"]["removed_points"] == 300
        assert calls == [("revoke", 14, 7), ("publish",)] and row.status == "revoked"

    async def test_nothing_awarded_409(self, client, monkeypatch):
        _wire(monkeypatch, _S(), _svc(row=_row(status="pending")))
        r = await client.post("/api/v1/events/1/clan-points/revoke", json={"confirm": "REVOKE"})
        assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Public read model: the awarded block
# --------------------------------------------------------------------------- #
def _award(pid, kind, amount, *, team_id=10, place=None, hours=None):
    return SimpleNamespace(player_id=pid, kind=kind, amount=amount, team_id=team_id,
                           place=place, hours=hours)


class TestAwardedBlock:
    def _rows(self):
        return [
            (_award(1, "placement", 100, place=1), "Alpha"),
            (_award(1, "participation", 12, hours=6.0), "Alpha"),
            (_award(2, "placement", 100, place=1), "Bravo"),
            (_award(3, "participation", 4, team_id=20, hours=2.0), "Charlie"),
        ]

    def test_folds_kinds_per_player_best_first(self):
        s = _S(self._rows())
        out = evp._awarded_block(s, _event(), 14, team_names={10: "Red", 20: "Blue"},
                                 admin=False, reveal_participation=True, hidden=set())
        assert (out["total_points"], out["players"], out["participation_players"]) == (216, 3, 2)
        assert [r["player_name"] for r in out["rows"]] == ["Alpha", "Bravo", "Charlie"]
        assert out["rows"][0] == {
            "player_id": 1, "player_name": "Alpha", "team_id": 10, "team_name": "Red",
            "place": 1, "placement": 100, "participation": 12, "hours": 6.0, "total": 112}

    def test_admin_only_ehe_hides_per_player_participation(self):
        s = _S(self._rows())
        out = evp._awarded_block(s, _event(), 14, team_names={}, admin=False,
                                 reveal_participation=False, hidden=set())
        # Totals stay whole; per-player participation (and Charlie, who only
        # earned participation) is withheld.
        assert out["participation_points"] == 16
        assert [r["player_name"] for r in out["rows"]] == ["Alpha", "Bravo"]
        assert out["rows"][0]["participation"] is None and out["rows"][0]["hours"] is None
        assert out["rows"][0]["total"] == 100

    def test_admins_see_everything(self):
        s = _S(self._rows())
        out = evp._awarded_block(s, _event(), 14, team_names={}, admin=True,
                                 reveal_participation=False, hidden={2})
        assert len(out["rows"]) == 3
        assert any(r["player_name"] == "Bravo" for r in out["rows"])

    def test_hidden_players_masked_publicly(self):
        s = _S(self._rows())
        out = evp._awarded_block(s, _event(), 14, team_names={}, admin=False,
                                 reveal_participation=True, hidden={2})
        masked = [r for r in out["rows"] if r["player_name"] == "Hidden player"]
        assert len(masked) == 1 and masked[0]["player_id"] is None
