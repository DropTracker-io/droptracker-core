"""Unit tests for the plugin event window composer (api/routes/plugin_events.py).
Pure: fake events and an already-built ``_detail`` payload, no DB."""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from api.routes import plugin_events as pe

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# The conftest stubs the services package; the composer's lazy imports get
# the real display helpers and task descriptions.
_display = _load("_plugin_display_for_pe_test", "services", "plugin_display.py")
_notifications = _load("_plugin_notifications_for_pe_test", "services", "plugin_notifications.py")
_board_image = _load("_event_board_image_for_pe_test", "services", "event_board_image.py")


@pytest.fixture(autouse=True)
def _real_services(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.plugin_display", _display)
    monkeypatch.setitem(sys.modules, "services.plugin_notifications", _notifications)
    monkeypatch.setitem(sys.modules, "services.event_board_image", _board_image)

NOW = datetime(2026, 10, 26, 12, 0, 0)


def _ev(**kw):
    base = dict(id=1, name="Spooky Bingo", kind="bingo", has_bingo=True, status="active",
                starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=2, hours=4),
                ended_at=None, activated_at=NOW - timedelta(days=1), group_id=5,
                tasks_visibility="public", description=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _detail(**kw):
    base = {
        "teams": [
            {"id": 10, "name": "Red", "color": "#ff0000", "score": 12, "member_count": 3,
             "members": [{"player_name": "Zezima", "role": "leader"}]},
            {"id": 11, "name": "Blue", "color": "#0000ff", "score": 30.5, "member_count": 2,
             "members": []},
        ],
        "tasks": [
            {"id": 1, "type": "item_collection", "label": "Get a <b>Twisted bow</b>",
             "target": None, "target_value": 1, "points": 5, "config": None,
             "tile": {"badge": "ITEM", "value": None, "icon_item_id": 20997}},
            {"id": 2, "type": "kc_target", "label": "Zulrah KC", "target": "Zulrah",
             "target_value": 100, "points": 0, "config": None, "tile": {}},
            {"id": 3, "type": "competition", "label": "managed", "managed": True,
             "target_value": 0, "tile": {}},
        ],
        "progress": [
            {"task_id": 1, "team_id": 11, "progress": 1, "completed": True},
            {"task_id": 2, "team_id": 10, "progress": 40, "completed": False},
            {"task_id": 2, "team_id": 11, "progress": 250, "completed": False,
             "completions": 2},
        ],
        "tasks_hidden": False,
        "prize_pot": {"enabled": False},
    }
    base.update(kw)
    return base


def test_subtitle_live_upcoming_and_ended():
    assert pe._subtitle(_ev(), "active", NOW) == "Bingo  ·  Ends in 2d 4h"
    up = _ev(starts_at=NOW + timedelta(hours=3))
    assert pe._subtitle(up, "draft", NOW) == "Bingo  ·  Starts in 3h 0m"
    past = _ev(ended_at=NOW - timedelta(days=2))
    assert pe._subtitle(past, "past", NOW) == "Bingo  ·  Ended 2 days ago"


def test_bar_wording_binary_counted_done_and_laps():
    task = {"type": "kc_target", "target_value": 100}
    assert pe._bar(task, None, "Red", "#ff0000")["text"] == "0 / 100"
    assert pe._bar(task, {"progress": 40}, "Red", None)["text"] == "40 / 100"
    assert pe._bar({"type": "skill_target"}, None, "Red", None)["text"] == "Not yet"
    done = pe._bar(task, {"progress": 100, "completed": True}, "Red", None)
    assert done["done"] and done["text"] == "Done"
    lapped = pe._bar(task, {"progress": 250, "completions": 2}, "Blue", None)
    assert lapped["have"] == 50 and lapped["text"] == "50 / 100  ×2"


def test_need_prefers_server_target_then_pb_config():
    assert pe._need({"type": "kc_target", "target_value": 5}, {"target": 9}) == 9
    assert pe._need({"type": "pb_target", "config": {"mode": "times", "need": 3}}, None) == 3
    assert pe._need({"type": "pb_target", "config": {"mode": "whole_team"}}, None) == 1


def test_task_items_strip_tags_skip_managed_and_bar_every_team():
    items = pe._task_items(_detail())
    assert [i["label"] for i in items] == ["Get a Twisted bow", "Zulrah KC"]
    assert items[0]["icon_item_id"] == 20997 and items[0]["points"] == "5 pts"
    assert [b["label"] for b in items[1]["bars"]] == ["Red", "Blue"]
    only_red = pe._task_items(_detail(), {10})
    assert all(len(i["bars"]) == 1 for i in only_red)


def test_standings_rank_by_score_and_link_team_pages():
    rows = pe._standings_rows(_detail())
    assert [r["cells"][1] for r in rows] == ["Blue", "Red"]
    assert rows[0]["cells"][0] == "1st" and rows[0]["cells"][2] == "30.5"
    assert rows[0]["page"] == "team:11" and rows[0]["color"] == "#0000ff"


def test_apply_viewer_marks_only_own_team_and_drops_markers():
    payload = {"blocks": [pe.table([pe.column("Team")], pe._standings_rows(_detail())),
                          {"type": "tasks", "items": pe._task_items(_detail())}]}
    out = pe.apply_viewer(payload, 10)
    rows = out["blocks"][0]["rows"]
    assert [r.get("highlight", False) for r in rows] == [False, True]
    assert "_team" not in str(out)
    out2 = pe.apply_viewer({"rows": [pe.row(["x"], team_id=10)]}, None)
    assert out2 == {"rows": [{"cells": ["x"]}]}


def test_pages_by_kind():
    keys = lambda ev, d=None: [k for k, _ in pe._pages(ev, d or _detail(), False)]  # noqa: E731
    assert keys(_ev()) == ["overview", "tasks", "board", "standings", "players", "history"]
    assert keys(_ev(kind="standard", has_bingo=False)) == [
        "overview", "tasks", "standings", "players", "history"]
    assert keys(_ev(kind="botw", has_bingo=False)) == [
        "overview", "board", "standings", "history"]
    # A held-back board has no page; a draft has no history yet.
    assert "board" not in keys(_ev(tasks_visibility="admins"))
    assert "history" not in keys(_ev(status="draft", starts_at=NOW + timedelta(days=1)))


def test_team_page_unknown_team_is_missing():
    try:
        pe._team_page(_ev(), _detail(), 999, NOW)
    except pe._Missing:
        pass
    else:
        raise AssertionError("expected _Missing")


def test_team_page_lists_roster_and_own_tasks():
    blocks = pe._team_page(_ev(kind="standard"), _detail(), 10, NOW)
    assert blocks[0] == {"type": "heading", "text": "Red"}
    assert blocks[1]["items"][0]["value"] == "2nd of 2"
    roster = next(b for b in blocks if b["type"] == "table")
    assert roster["rows"][0]["cells"] == ["Zezima", "Leader"]
    tasks = next(b for b in blocks if b["type"] == "tasks")
    assert all(len(i["bars"]) == 1 for i in tasks["items"])


def test_team_colors_fall_back_to_the_site_palette_in_id_order(monkeypatch):
    real = _load("_event_team_discord_for_pe_test", "services", "event_team_discord.py")
    monkeypatch.setitem(sys.modules, "services.event_team_discord", real)
    colors = pe.team_colors([{"id": 9, "color": None}, {"id": 3, "color": None},
                             {"id": 5, "color": "#123456"}])
    assert colors == {3: real.TEAM_PALETTE[0], 5: "#123456", 9: real.TEAM_PALETTE[2]}


def test_history_folds_repeated_lines(monkeypatch):
    import web_api.routes.events as web_events

    entry = {"player_name": "ranarr ron", "task_label": "GWD pet or 5k KC",
             "team_id": 10, "team_name": "Red", "points": 0,
             "created_at": int(NOW.timestamp()) - 120}
    other = dict(entry, player_name="Yung Beez")
    monkeypatch.setattr(web_events, "completion_history_payload",
                        lambda *a, **k: {"entries": [entry, entry, entry, other],
                                         "meta": {"total": 4}})
    blocks = pe._history_page(None, _ev(), None, NOW, {10: "#ff0000"})
    rows = pe.apply_viewer({"b": blocks}, 10)["b"][0]["rows"]
    assert [r["cells"][0] for r in rows] == ["ranarr ron completed GWD pet or 5k KC  ×3",
                                             "Yung Beez completed GWD pet or 5k KC"]
    assert rows[0]["cells"][1:] == ["Red", "", "2m ago"]
    assert rows[0]["color"] == "#ff0000" and rows[0]["highlight"] is True
    assert len(blocks) == 1  # nothing more to show on the website


def test_board_page_starts_on_own_team_and_links_the_others():
    detail = _detail()
    for t, c in zip(detail["teams"], ("#ff0000", "#0000ff")):
        t["color"] = c
    blocks = pe._board_page(_ev(), detail, SimpleNamespace(id=10))
    assert blocks[0] == {"type": "board", "team_id": 10}
    rows = blocks[1]["rows"]
    assert [r["cells"][0] for r in rows] == ["All teams", "Blue", "Red"]
    assert [r.get("page") for r in rows] == ["board:0", "board:11", None]
    assert rows[2]["highlight"] is True
    everyone = pe._board_page(_ev(), detail, None, 0)
    assert everyone[0] == {"type": "board", "team_id": None}
    assert pe._board_page(_ev(kind="botw"), detail, None) == [{"type": "board"}]
    with pytest.raises(pe._Missing):
        pe._board_page(_ev(), detail, None, 999)
