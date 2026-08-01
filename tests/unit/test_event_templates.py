"""Event templates — snapshot, save, instantiate, sharing (save/rerun events).

Mirrors the scripted-session harness of ``test_event_auth_modes`` /
``test_event_team_edit_delete``: each ``_S(...)`` batch answers the next
query the handler issues, in order. ``_assert_event_admin`` is stubbed to a
no-op (its contract is covered by the auth tests); ``validate_task_payload``
is stubbed so instantiation tests script their own pass/fail per task. The
conftest stubs ``db``, so ORM classes and enum tuples are restored with
fakes/real values via the autouse fixture (test_event_task_library_visibility
pattern).
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.event_templates as etr
from web_api.common import ProblemException


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def offset(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def first(self):
        return self._r[0] if self._r else None

    def all(self):
        return list(self._r)


class _S:
    """Scripted session: query() returns the next batch, in order; running
    out means the handler issued an unexpected extra query."""

    def __init__(self, *batches):
        self._batches = list(batches)
        self.added = []
        self.committed = False
        self.synced = []

    def query(self, *a, **k):
        assert self._batches, "unexpected extra query"
        return _Q(self._batches.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True


class _SessionCM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


class _ECol:
    """Column stand-in usable in filter/order_by expressions."""

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0

    def in_(self, other):
        return True

    def is_(self, other):
        return True

    def like(self, other):
        return True

    def __or__(self, other):
        return True

    def desc(self):
        return self

    def asc(self):
        return self


class FakeTemplate:
    id = _ECol()
    name = _ECol()
    description = _ECol()
    group_id = _ECol()
    visibility = _ECol()
    active = _ECol()
    times_used = _ECol()

    def __init__(self, **kw):
        base = dict(
            id=101, name=None, description=None, source_event_id=None,
            group_id=None, created_by_user_id=None, visibility="private",
            mode="standard", has_bingo=False, board_size=5, task_count=0,
            team_count=0, schema_version=1, times_used=0, payload="",
            active=True, created_at=None, updated_at=None,
        )
        base.update(kw)
        self.__dict__.update(base)


class FakeEvent:
    id = _ECol()

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = 55


class FakeTask:
    id = _ECol()
    event_id = _ECol()
    _next = 900

    def __init__(self, **kw):
        FakeTask._next += 1
        self.__dict__.update(kw)
        self.id = FakeTask._next


class FakeTeam:
    id = _ECol()
    event_id = _ECol()

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeCell:
    id = _ECol()
    event_id = _ECol()
    idx = _ECol()

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeAudit:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _default_validate(s, body):
    cfg = body.get("config")
    return {
        "target": (body.get("target") or None),
        "target_value": body.get("target_value"),
        "config": cfg if (cfg is None or isinstance(cfg, str)) else json.dumps(cfg),
    }


_TASK_TYPES = (
    "item_collection", "kc_target", "xp_target", "ehp_target", "ehb_target",
    "pb_target", "skill_target", "loot_value", "custom",
)


@pytest.fixture(autouse=True)
def _unstub(monkeypatch):
    """Restore real semantics for the pieces the conftest's db stub breaks."""
    monkeypatch.setattr(etr, "EVENT_TASK_TYPES", _TASK_TYPES)
    monkeypatch.setattr(etr, "EVENT_TASK_VISIBILITIES", ("public", "private"))
    monkeypatch.setattr(etr, "EVENT_TEMPLATE_SCHEMA_VERSION", 1)
    monkeypatch.setattr(etr, "EventTemplate", FakeTemplate)
    monkeypatch.setattr(etr, "Event", FakeEvent)
    monkeypatch.setattr(etr, "EventTask", FakeTask)
    monkeypatch.setattr(etr, "EventTeam", FakeTeam)
    monkeypatch.setattr(etr, "EventBingoCell", FakeCell)
    monkeypatch.setattr(etr, "Group", MagicMock())
    monkeypatch.setattr(etr, "AuditLog", FakeAudit)
    monkeypatch.setattr(etr, "func", MagicMock())
    monkeypatch.setattr(etr, "sa_or", lambda *a: True)
    monkeypatch.setattr(
        "web_api.routes.event_task_validation.validate_task_payload",
        _default_validate,
    )
    FakeTask._next = 900


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session, *, user_id=7, superadmin=False, admin_gids=(42,)):
    monkeypatch.setattr(etr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(etr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(etr, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(etr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(etr, "load_user", lambda s, uid: "USER")
    monkeypatch.setattr(etr, "is_superadmin", lambda u: superadmin)
    monkeypatch.setattr(etr, "_viewer_admin_group_ids", lambda s, uid: set(admin_gids))
    monkeypatch.setattr(etr, "_sync_event_guilds", lambda s, ev: session.synced.append(ev))


def _event(**kw):
    base = dict(
        id=1, group_id=42, name="Winter Bingo", description="Best event",
        status="past", starts_at=None, ends_at=None, has_bingo=True,
        formation_mode="self_join", requires_confirmation=True,
        submission_policy="api_only", join_code="secret", discord_guild_id="999",
        board_size=3, bonus_line_points=5, bonus_blackout_points=25,
        activated_at=None, ended_at=None, mode="standard",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _task(id, label, *, type="item_collection", target="Abyssal whip",
          target_value=1, points=10, requires_confirmation=False,
          visibility="public", config=None):
    return SimpleNamespace(
        id=id, event_id=1, type=type, label=label, target=target,
        target_value=target_value, points=points,
        requires_confirmation=requires_confirmation, visibility=visibility,
        config=config,
    )


def _team(id, name):
    return SimpleNamespace(id=id, event_id=1, name=name, score=17, group_id=None)


def _cell(idx, label, task_id=None):
    return SimpleNamespace(id=idx + 500, event_id=1, idx=idx, label=label, task_id=task_id)


def _payload(**event_overrides):
    """A well-formed v1 template payload with 2 tasks, 1 team, 2×2-ish board."""
    event = dict(
        description="Best event", formation_mode="self_join",
        requires_confirmation=True, submission_policy="api_only",
        has_bingo=True, board_size=3, bonus_line_points=5,
        bonus_blackout_points=25, mode="standard",
    )
    event.update(event_overrides)
    return {
        "version": 1,
        "event": event,
        "tasks": [
            {"type": "item_collection", "label": "Whip", "target": "Abyssal whip",
             "target_value": 1, "points": 10, "requires_confirmation": False,
             "visibility": "public", "config": None},
            {"type": "kc_target", "label": "Zulrah x50", "target": "Zulrah",
             "target_value": 50, "points": 5, "requires_confirmation": True,
             "visibility": "private", "config": None},
        ],
        "teams": [{"name": "Red"}, {"name": "Blue"}],
        "bingo": {"size": 3, "cells": [
            {"idx": 0, "label": "Whip", "task_ref": 0},
            {"idx": 1, "label": "Zulrah x50", "task_ref": 1},
            {"idx": 2, "label": "Free space", "task_ref": None},
        ]},
    }


def _template(**kw):
    base = dict(
        id=33, name="Winter Bingo", description=None, source_event_id=1,
        group_id=42, created_by_user_id=7, visibility="private",
        mode="standard", has_bingo=True, board_size=3, task_count=2,
        team_count=2, schema_version=1, times_used=0,
        payload=json.dumps(_payload()), active=True,
        created_at=datetime(2026, 7, 1), updated_at=datetime(2026, 7, 2),
    )
    base.update(kw)
    return FakeTemplate(**base)


# ── snapshot_event ────────────────────────────────────────────────────────────

class TestSnapshot:
    def test_round_trip_structure(self):
        tasks = [_task(11, "Whip"), _task(12, "Zulrah x50", type="kc_target",
                                          target="Zulrah", target_value=50)]
        teams = [_team(3, "Red")]
        cells = [_cell(0, "Whip", task_id=11), _cell(1, "Zulrah x50", task_id=12),
                 _cell(2, "Free space", task_id=None)]
        s = _S(tasks, teams, cells)
        payload = etr.snapshot_event(s, _event())

        assert payload["version"] == 1
        # Structure captured…
        assert payload["event"] == {
            "description": "Best event", "formation_mode": "self_join",
            "requires_confirmation": True, "submission_policy": "api_only",
            "has_bingo": True, "board_size": 3, "bonus_line_points": 5,
            "bonus_blackout_points": 25, "mode": "standard", "kind": "standard",
            # Organizer settings carried since the audit fix (absent attrs on
            # the fake event snapshot to their defaults).
            "buyins_enabled": False, "prize_config": None,
            "leadership_config": None, "message_config": None,
            # Recurring schedule rule (web82a) — recompiled against the new
            # run's dates on instantiate; None for a continuous event.
            "schedule": None,
        }
        assert [t["label"] for t in payload["tasks"]] == ["Whip", "Zulrah x50"]
        assert payload["teams"] == [{"name": "Red"}]
        # …cells reference tasks by payload index, never row ids.
        assert [c["task_ref"] for c in payload["bingo"]["cells"]] == [0, 1, None]
        assert payload["bingo"]["size"] == 3
        # Runtime state never leaks into the snapshot.
        flat = json.dumps(payload)
        assert "join_code" not in flat and "secret" not in flat
        assert "discord_guild_id" not in flat and "score" not in flat

    def test_no_bingo_skips_cell_query(self):
        s = _S([_task(11, "Whip")], [])
        payload = etr.snapshot_event(s, _event(has_bingo=False))
        assert payload["bingo"] is None

    def test_strips_bingo_auto_marker(self):
        tasks = [
            _task(11, "A", config='{"bingo_auto": true, "kind": "any_of"}'),
            _task(12, "B", config='{"bingo_auto": true}'),
            _task(13, "C", config='{"kind": "all_of"}'),
        ]
        s = _S(tasks, [], [])
        payload = etr.snapshot_event(s, _event())
        configs = [t["config"] for t in payload["tasks"]]
        assert json.loads(configs[0]) == {"kind": "any_of"}
        assert configs[1] is None
        assert json.loads(configs[2]) == {"kind": "all_of"}


# ── POST /events/{id}/save-template ──────────────────────────────────────────

class TestSaveTemplate:
    async def test_save_creates_template_row(self, client, monkeypatch):
        tasks = [_task(11, "Whip"), _task(12, "Zulrah x50")]
        s = _S([_event()], tasks, [_team(3, "Red")],
               [_cell(0, "Whip", 11)], [])  # last: no existing template
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/save-template",
                              json={"name": "Winter Bingo", "visibility": "public"})
        assert r.status_code == 200
        assert (await r.get_json()) == {"id": 101}
        tmpl = next(a for a in s.added if isinstance(a, FakeTemplate))
        assert tmpl.group_id == 42
        assert tmpl.visibility == "public"
        assert tmpl.task_count == 2 and tmpl.team_count == 1
        assert tmpl.source_event_id == 1 and tmpl.created_by_user_id == 7
        assert json.loads(tmpl.payload)["version"] == 1
        audit = next(a for a in s.added if isinstance(a, FakeAudit))
        assert audit.action == "event.template.save"
        assert s.committed

    async def test_save_upserts_same_name(self, client, monkeypatch):
        existing = _template(id=33, visibility="public")
        s = _S([_event()], [], [], [], [existing])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/save-template",
                              json={"name": "winter BINGO"})
        assert r.status_code == 200
        assert (await r.get_json()) == {"id": 33}
        # Updated in place — no second template row, default flips it private.
        assert not any(isinstance(a, FakeTemplate) for a in s.added)
        assert existing.visibility == "private"
        assert existing.name == "winter BINGO"

    async def test_save_can_exclude_teams(self, client, monkeypatch):
        s = _S([_event(has_bingo=False)], [_task(11, "Whip")], [_team(3, "Red")], [])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/save-template",
                              json={"name": "T", "include_teams": False})
        assert r.status_code == 200
        tmpl = next(a for a in s.added if isinstance(a, FakeTemplate))
        assert tmpl.team_count == 0
        assert json.loads(tmpl.payload)["teams"] == []

    async def test_save_blank_name_422(self, client, monkeypatch):
        _wire(monkeypatch, _S())
        r = await client.post("/api/v1/events/1/save-template", json={"name": "  "})
        assert r.status_code == 422

    async def test_save_bad_visibility_422(self, client, monkeypatch):
        _wire(monkeypatch, _S())
        r = await client.post("/api/v1/events/1/save-template",
                              json={"name": "T", "visibility": "friends"})
        assert r.status_code == 422


# ── POST /event-templates/{id}/instantiate ───────────────────────────────────

class TestInstantiate:
    async def test_creates_fresh_draft(self, client, monkeypatch):
        group = SimpleNamespace(group_id=42, guild_id="777")
        s = _S([_template(visibility="private")], [group])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42, "starts_at": 1780000000})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["id"] == 55 and body["skipped_tasks"] == []

        ev = next(a for a in s.added if isinstance(a, FakeEvent))
        assert ev.status == "draft" and ev.mode == "standard"
        assert ev.join_code is None and ev.ping_config is None
        assert ev.formation_mode == "self_join" and ev.submission_policy == "api_only"
        assert ev.discord_guild_id == "777"
        assert ev.has_bingo is True and ev.board_size == 3

        tasks = [a for a in s.added if isinstance(a, FakeTask)]
        assert [t.label for t in tasks] == ["Whip", "Zulrah x50"]
        assert all(t.event_id == 55 for t in tasks)

        cells = [a for a in s.added if isinstance(a, FakeCell)]
        # task_ref indexes resolved to the NEW task ids; free cell unbound.
        assert [c.task_id for c in cells] == [tasks[0].id, tasks[1].id, None]

        teams = [a for a in s.added if isinstance(a, FakeTeam)]
        assert [t.name for t in teams] == ["Red", "Blue"]
        assert all(t.score == 0 for t in teams)

        assert s.synced and s.synced[0] is ev  # guild mirror parity with POST /events
        audit = next(a for a in s.added if isinstance(a, FakeAudit))
        assert audit.action == "event.template.instantiate"
        assert s.committed

    async def test_skipped_tasks_reported_and_cells_unbound(self, client, monkeypatch):
        def _picky_validate(sess, body):
            if body.get("target") == "Zulrah":
                raise ProblemException(422, "Unknown NPC", "'Zulrah' was renamed.")
            return _default_validate(sess, body)

        monkeypatch.setattr(
            "web_api.routes.event_task_validation.validate_task_payload",
            _picky_validate,
        )
        group = SimpleNamespace(group_id=42, guild_id=None)
        s = _S([_template()], [group])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["skipped_tasks"] == [
            {"index": 1, "label": "Zulrah x50", "reason": "'Zulrah' was renamed."}
        ]
        tasks = [a for a in s.added if isinstance(a, FakeTask)]
        assert [t.label for t in tasks] == ["Whip"]
        cells = [a for a in s.added if isinstance(a, FakeCell)]
        # The skipped task's cell survives, unbound — not silently a free cell.
        assert [c.task_id for c in cells] == [tasks[0].id, None, None]
        assert cells[1].label == "Zulrah x50"

    async def test_include_teams_false(self, client, monkeypatch):
        group = SimpleNamespace(group_id=42, guild_id=None)
        s = _S([_template()], [group])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42, "include_teams": False})
        assert r.status_code == 200
        assert not any(isinstance(a, FakeTeam) for a in s.added)

    async def test_cvc_template_resets_to_standard(self, client, monkeypatch):
        tmpl = _template(payload=json.dumps(_payload(mode="clan_vs_clan")))
        group = SimpleNamespace(group_id=42, guild_id=None)
        s = _S([tmpl], [group])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42})
        assert r.status_code == 200
        ev = next(a for a in s.added if isinstance(a, FakeEvent))
        assert ev.mode == "standard"

    async def test_bumps_times_used(self, client, monkeypatch):
        tmpl = _template(times_used=4)
        group = SimpleNamespace(group_id=42, guild_id=None)
        s = _S([tmpl], [group])
        _wire(monkeypatch, s)
        await client.post("/api/v1/event-templates/33/instantiate",
                          json={"group_id": 42})
        assert tmpl.times_used == 5

    async def test_private_template_of_other_group_404(self, client, monkeypatch):
        s = _S([_template(group_id=99, visibility="private")])
        _wire(monkeypatch, s, admin_gids=(42,))
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42})
        assert r.status_code == 404

    async def test_public_template_of_other_group_ok(self, client, monkeypatch):
        group = SimpleNamespace(group_id=42, guild_id=None)
        s = _S([_template(group_id=99, visibility="public")], [group])
        _wire(monkeypatch, s, admin_gids=(42,))
        r = await client.post("/api/v1/event-templates/33/instantiate",
                              json={"group_id": 42})
        assert r.status_code == 200


# ── GET /event-templates (+ detail) ──────────────────────────────────────────

class TestListAndDetail:
    async def test_list_returns_summaries(self, client, monkeypatch):
        s = _S([_template(times_used=2)])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/event-templates")
        assert r.status_code == 200
        [row] = await r.get_json()
        assert row["id"] == 33 and row["name"] == "Winter Bingo"
        assert row["task_count"] == 2 and row["team_count"] == 2
        assert row["times_used"] == 2 and row["visibility"] == "private"
        assert "payload" not in row  # snapshots stay server-side

    async def test_list_requires_some_admin(self, client, monkeypatch):
        _wire(monkeypatch, _S(), admin_gids=())
        r = await client.get("/api/v1/event-templates")
        assert r.status_code == 403

    async def test_list_group_filter_needs_that_group(self, client, monkeypatch):
        _wire(monkeypatch, _S(), admin_gids=(42,))
        r = await client.get("/api/v1/event-templates?groupId=99")
        assert r.status_code == 403

    async def test_detail_includes_preview(self, client, monkeypatch):
        s = _S([_template()])
        _wire(monkeypatch, s)
        r = await client.get("/api/v1/event-templates/33")
        assert r.status_code == 200
        body = await r.get_json()
        assert [t["label"] for t in body["preview"]["tasks"]] == ["Whip", "Zulrah x50"]
        assert body["preview"]["teams"] == ["Red", "Blue"]
        assert body["preview"]["formation_mode"] == "self_join"

    async def test_detail_private_other_group_404(self, client, monkeypatch):
        s = _S([_template(group_id=99, visibility="private")])
        _wire(monkeypatch, s, admin_gids=(42,))
        r = await client.get("/api/v1/event-templates/33")
        assert r.status_code == 404


# ── PATCH / DELETE /event-templates/{id} ─────────────────────────────────────

class TestManage:
    async def test_patch_updates_and_audits(self, client, monkeypatch):
        tmpl = _template()
        s = _S([tmpl])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/event-templates/33",
                               json={"name": "Summer Bingo", "visibility": "public"})
        assert r.status_code == 200
        assert tmpl.name == "Summer Bingo" and tmpl.visibility == "public"
        audit = next(a for a in s.added if isinstance(a, FakeAudit))
        assert audit.action == "event.template.update"
        assert s.committed

    async def test_patch_noop_skips_audit(self, client, monkeypatch):
        tmpl = _template()
        s = _S([tmpl])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/event-templates/33", json={})
        assert r.status_code == 200
        assert s.added == [] and not s.committed

    async def test_patch_forbidden_for_non_owner(self, client, monkeypatch):
        s = _S([_template(group_id=99)])
        _wire(monkeypatch, s, admin_gids=(42,))
        r = await client.patch("/api/v1/event-templates/33", json={"name": "X"})
        assert r.status_code == 403

    async def test_sitewide_template_superadmin_only(self, client, monkeypatch):
        s = _S([_template(group_id=None, visibility="public")])
        _wire(monkeypatch, s, admin_gids=(42,))
        r = await client.patch("/api/v1/event-templates/33", json={"name": "X"})
        assert r.status_code == 403

    async def test_delete_is_soft(self, client, monkeypatch):
        tmpl = _template()
        s = _S([tmpl])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/event-templates/33")
        assert r.status_code == 200
        assert tmpl.active is False
        audit = next(a for a in s.added if isinstance(a, FakeAudit))
        assert audit.action == "event.template.delete"
        assert s.committed


# ── _normalize_payload ────────────────────────────────────────────────────────

class TestNormalizePayload:
    def test_accepts_current_version(self):
        assert etr._normalize_payload(json.dumps(_payload()))["version"] == 1

    @pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"version": 1})])
    def test_rejects_corrupt(self, raw):
        with pytest.raises(ProblemException) as exc:
            etr._normalize_payload(raw)
        assert exc.value.status == 500

    def test_rejects_unknown_version(self):
        raw = json.dumps({**_payload(), "version": 999})
        with pytest.raises(ProblemException) as exc:
            etr._normalize_payload(raw)
        assert exc.value.status == 500
