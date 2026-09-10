"""Per-event board/task visibility (web112a).

An event can keep its task list, bingo cells, board-game tiles and board
images to its organisers — for an event played blind, or a board held back
until the reveal. Scoring, standings and completion notifications carry on;
only what a participant is SHOWN changes. Same pure-helper approach as
test_event_effort_visibility.py: the gates and scrubbers, not the routes.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import web_api.routes.event_board as eb
import web_api.routes.realtime as rt
from web_api.common import ProblemException
from web_api.routes import events as ev_routes

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    """The conftest stubs ``services``; load the real module by file path."""
    if module_name in sys.modules and getattr(sys.modules[module_name], "__file__", None):
        return sys.modules[module_name]
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


board_image = _load("services.event_board_image", "services", "event_board_image.py")
plugin = _load("_plugin_notifications_for_tv_test", "services", "plugin_notifications.py")


class _Event:
    def __init__(self, visibility=None, **kw):
        if visibility is not None:
            self.tasks_visibility = visibility
        for k, v in kw.items():
            setattr(self, k, v)


# ── the setting ──────────────────────────────────────────────────────────────

class TestVisibilityCoercion:
    @pytest.mark.parametrize("raw,expected", [
        ("public", "public"),
        ("admins", "admins"),
        ("ADMINS", "admins"),
        ("  admins  ", "admins"),
    ])
    def test_known_values(self, raw, expected):
        assert ev_routes._tasks_visibility_value(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "nonsense", 7, "private", "hidden"])
    def test_unknown_values_fall_back_to_public(self, raw):
        # Forgiving on purpose: a stray value must not fail an event save, and
        # showing the board is the safe landing spot.
        assert ev_routes._tasks_visibility_value(raw) == "public"


class TestVisibilityGate:
    def test_public_events_show_everyone(self):
        assert ev_routes._tasks_visible(MagicMock(), None, _Event("public")) is True

    def test_missing_column_reads_as_public(self):
        # Pre-migration rows / legacy objects keep working.
        assert ev_routes._tasks_kept_to_admins(_Event()) is False
        assert ev_routes._tasks_visible(MagicMock(), None, _Event()) is True

    def test_admins_only_hides_it_from_anonymous_viewers(self):
        with patch.object(ev_routes, "_is_event_admin", return_value=False):
            assert ev_routes._tasks_visible(MagicMock(), None, _Event("admins")) is False

    def test_admins_only_still_shows_the_event_admin(self):
        with patch.object(ev_routes, "_is_event_admin", return_value=True) as gate:
            assert ev_routes._tasks_visible(MagicMock(), 42, _Event("admins")) is True
        gate.assert_called_once()

    def test_public_never_pays_for_the_admin_lookup(self):
        with patch.object(ev_routes, "_is_event_admin") as gate:
            ev_routes._tasks_visible(MagicMock(), None, _Event("public"))
        gate.assert_not_called()

    def test_denial_is_a_reasoned_403(self):
        # The event exists and the viewer may know it; only its tasks are
        # withheld — so a code the site can explain, not a 404.
        with pytest.raises(ProblemException) as e:
            ev_routes._deny_hidden_tasks()
        assert e.value.status == 403
        assert e.value.extra.get("code") == "event_tasks_hidden"


# ── the board game keeps its track, loses its tiles ──────────────────────────

class TestBoardGameTiles:
    def _tile(self):
        return SimpleNamespace(idx=3, x=10.5, y=20.0, label="Kill Zulrah",
                               difficulty="hard", task_id=91, tile_kind="normal")

    def test_plain_row_names_the_task(self):
        row = eb._tile_row(self._tile(), {91: "Zulrah x10"})
        assert row["task_label"] == "Zulrah x10"
        assert row["label"] == "Kill Zulrah"

    def test_concealed_row_keeps_geometry_and_kind_only(self):
        row = eb._tile_row(self._tile(), {91: "Zulrah x10"}, conceal=True)
        assert (row["idx"], row["x"], row["y"], row["tile_kind"]) == (3, 10.5, 20.0, "normal")
        assert row["label"] is None
        assert row["difficulty"] is None
        assert row["task_id"] is None
        assert row["task_label"] is None
        # Same key set either way — the client's tile shape does not change.
        assert set(row) == set(eb._tile_row(self._tile(), {}))


# ── the picture never leaves the organisers ──────────────────────────────────

class TestBoardImages:
    def test_public_board_is_drawn(self):
        assert board_image.board_kept_to_admins(_Event("public", kind="bingo")) is False
        assert board_image.board_kept_to_admins(_Event(kind="bingo")) is False

    def test_hidden_board_is_not(self):
        assert board_image.board_kept_to_admins(_Event("admins", kind="bingo")) is True
        assert board_image.board_kept_to_admins(_Event("admins", kind="board_game")) is True

    def test_competition_snapshots_name_players_not_tasks(self):
        for kind in ("sotw", "botw"):
            assert board_image.board_kept_to_admins(_Event("admins", kind=kind)) is False

    def test_hidden_board_yields_no_render_inputs_without_reading_anything(self, monkeypatch):
        # None before any signature runs — so no DB, and every consumer
        # (announcements, leaderboard post, team channels, in-game pop-out)
        # sees "no visual board".
        monkeypatch.setattr(board_image, "_bingo_signature",
                            lambda *a, **k: pytest.fail("signature must not run"))
        ev = SimpleNamespace(id=7, has_bingo=True, kind="bingo", tasks_visibility="admins")
        assert board_image._collect_render_inputs(object(), ev) is None

    def test_an_admin_asking_by_hand_still_gets_the_board(self, monkeypatch):
        monkeypatch.setattr(board_image, "_bingo_signature",
                            lambda s, e, team_id=None: {"kind": "bingo", "x": 1})
        ev = SimpleNamespace(id=7, has_bingo=True, kind="bingo", tasks_visibility="admins")
        out = board_image._collect_render_inputs(object(), ev, for_admin=True)
        assert out["kind"] == "bingo"

    def test_public_board_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(board_image, "_bingo_signature",
                            lambda s, e, team_id=None: {"kind": "bingo", "x": 1})
        ev = SimpleNamespace(id=7, has_bingo=True, kind="bingo", tasks_visibility="public")
        assert board_image._collect_render_inputs(object(), ev)["kind"] == "bingo"


# ── the live frames lose their task names ────────────────────────────────────

class TestRealtimeScrub:
    def test_strips_every_task_naming_key_at_any_depth(self):
        frame = json.dumps({
            "v": 1, "type": "event_update", "scope": "event:7",
            "data": {"task_id": 91, "task_label": "Zulrah x10", "points": 5,
                     "team_score": 40, "cell_label": "B3",
                     "cells": [{"idx": 1, "cell_label": "A1"}],
                     "board": {"next_task_label": "Vorkath", "next_idx": 4},
                     "cell_labels": ["A1", "B2"]},
        })
        out = json.loads(rt.scrub_task_names(frame))
        assert "task_label" not in json.dumps(out)
        assert "cell_label" not in json.dumps(out)
        assert "next_task_label" not in json.dumps(out)
        # Everything the page refreshes on is still there.
        assert out["type"] == "event_update"
        assert out["data"]["task_id"] == 91
        assert out["data"]["points"] == 5
        assert out["data"]["team_score"] == 40
        assert out["data"]["cells"] == [{"idx": 1}]
        assert out["data"]["board"] == {"next_idx": 4}

    def test_non_json_passes_through(self):
        assert rt.scrub_task_names(": ping") == ": ping"

    def _install(self, monkeypatch, *, hidden, admin):
        import web_api.common as common

        ev = _Event("admins" if hidden else "public")
        fake = SimpleNamespace(query=lambda *a, **k: SimpleNamespace(
            filter=lambda *a, **k: SimpleNamespace(first=lambda: ev)))
        monkeypatch.setattr(common, "db_session", lambda: contextlib.nullcontext(fake))
        monkeypatch.setattr(ev_routes, "_is_event_admin", lambda s, uid, e: admin)

    def test_public_event_is_never_scrubbed(self, monkeypatch):
        self._install(monkeypatch, hidden=False, admin=False)
        assert rt._tasks_hidden_from(None, 7) is False

    def test_hidden_event_is_scrubbed_for_a_participant(self, monkeypatch):
        self._install(monkeypatch, hidden=True, admin=False)
        assert rt._tasks_hidden_from(42, 7) is True
        assert rt._tasks_hidden_from(None, 7) is True

    def test_hidden_event_is_not_scrubbed_for_its_admin(self, monkeypatch):
        self._install(monkeypatch, hidden=True, admin=True)
        assert rt._tasks_hidden_from(42, 7) is False

    def test_a_failed_lookup_scrubs(self, monkeypatch):
        import web_api.common as common

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(common, "db_session", _boom)
        assert rt._tasks_hidden_from(42, 7) is True

    def test_authorize_reports_which_scopes_to_scrub(self, monkeypatch):
        monkeypatch.setattr(rt, "optional_user_id", lambda: 42)
        monkeypatch.setattr(rt, "_may_watch_event", lambda uid, eid: True)
        monkeypatch.setattr(rt, "_tasks_hidden_from", lambda uid, eid: eid == 7)
        channels, scrubbed = asyncio.run(rt._authorize_channels("event:7,event:8,global"))
        assert channels == ["event:7", "event:8", "global"]
        assert scrubbed == {"event:7"}


# ── in-game ──────────────────────────────────────────────────────────────────

class TestPluginState:
    def test_setting_is_read_off_the_event(self):
        assert plugin.tasks_kept_to_admins(_Event("admins")) is True
        assert plugin.tasks_kept_to_admins(_Event("public")) is False
        assert plugin.tasks_kept_to_admins(_Event()) is False

    def test_blind_feed_keeps_the_item_but_not_the_task(self):
        # The map compose_event_state hands over when the event is blind: a
        # row still says who got what, credited to "Hidden task", and never
        # borrows the task's own icon.
        rows = [{"player_id": 1, "player_name": "Ra ine", "source_type": "drop",
                 "task_id": 91, "matched_target": "Tanzanite fang", "quantity": 1,
                 "proof_url": None, "date_received": "2026-09-10T10:00:00"},
                {"player_id": 2, "player_name": "joelhalen", "source_type": "manual",
                 "task_id": 91, "matched_target": None, "quantity": 1,
                 "proof_url": None, "date_received": "2026-09-10T10:01:00"}]
        blind = {91: {"label": "Hidden task", "type": "item_drop", "icon_path": None}}
        out = plugin.team_submission_entries(rows, blind, {"tanzanite fang": 12922})
        assert out[0]["source_name"] == "Hidden task"
        assert out[0]["display_name"] == "Tanzanite fang"
        assert out[0]["image_path"] == "itemdb/12922.png"
        assert out[1]["display_name"] == "Hidden task"
        assert "image_path" not in out[1]
