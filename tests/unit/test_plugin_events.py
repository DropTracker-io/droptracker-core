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


def test_task_cards_link_their_page_and_name_what_counts():
    detail = _detail()
    detail["tasks"][0]["config"] = {"kind": "all_of", "items": [
        "Twisted bow", {"name": "Dragon bones", "quantity": 3}, "Elysian sigil"]}
    items = pe._task_items(detail)
    assert items[0]["page"] == "task:1"
    assert items[0]["note"] == "Counts: Twisted bow, 3× Dragon bones, Elysian sigil"
    assert "note" not in items[1]  # a KC task has no item list


def test_needs_line_caps_length_and_counts_the_rest():
    many = [{"name": f"Item number {i} of a long set"} for i in range(9)]
    line = pe._needs_line(many)
    assert line.startswith("Counts: Item number 0 of a long set, ") and line.endswith(" +6 more")
    assert len(line) < 125
    assert pe._needs_line([]) is None
    capped = pe._needs_line([{"name": "A"}] * pe.REQUIREMENTS_LIMIT)
    assert capped.endswith(" and more")


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


def _session(task, teams):
    from db.models import EventTask

    return SimpleNamespace(query=lambda model: _Query([task] if model is EventTask else teams))


def _item(name, obtained, required=1, item_id=None, **kw):
    out = {"name": name, "icon": {"type": "item", "id": item_id, "name": name} if item_id else None,
           "required": required, "obtained": obtained, "satisfied": obtained >= required}
    out.update(kw)
    return out


def test_task_page_checklist_puts_own_team_first(monkeypatch):
    import web_api.routes.events as web_events

    states = {
        10: {"structure": "checklist", "groups": [{"mode": "all_of", "need": 2, "obtained": 1, "items": [
            _item("Twisted bow", 1, item_id=20997), _item("Dragon bones", 1, 3, pending=1)]}],
             "contributors": [{"player_name": "Zezima", "quantity": 2,
                               "items": [{"name": "Twisted bow", "quantity": 1},
                                         {"name": "Dragon bones", "quantity": 1}]}]},
        11: {"structure": "checklist", "groups": [{"mode": "all_of", "need": 2, "obtained": 0, "items": [
            _item("Twisted bow", 0, item_id=20997), _item("Dragon bones", 3, 3)]}],
             "contributors": []},
    }
    monkeypatch.setattr(web_events, "team_task_breakdown", lambda s, eid, task, team: states[team.id])
    teams = [SimpleNamespace(id=10), SimpleNamespace(id=11)]
    s = _session(SimpleNamespace(id=1), teams)
    blocks = pe._task_page(s, _ev(), _detail(), 1, SimpleNamespace(id=10))
    assert blocks[0] == {"type": "heading", "text": "Get a Twisted bow"}
    assert blocks[1]["type"] == "tasks"
    assert "page" not in blocks[1]["items"][0] and "note" not in blocks[1]["items"][0]
    assert [b["label"] for b in blocks[1]["items"][0]["bars"]] == ["Red", "Blue"]
    check = next(b for b in blocks if b["type"] == "table")
    assert [c["label"] for c in check["columns"]] == ["Item", "Need", "Red", "Blue"]
    bow, bones = check["rows"]
    assert bow["cells"] == ["Twisted bow", "1", "Done", "-"]
    assert bow["icon_item_id"] == 20997 and bow["color"] == pe.DONE_COLOR
    assert bones["cells"] == ["Dragon bones", "3×", "Pending review", "Done"]
    assert "icon_item_id" not in bones and "color" not in bones
    headings = [b["text"] for b in blocks if b["type"] == "heading"]
    assert "All of these  (1 / 2)" in headings and "Who has helped (Red)" in headings
    helped = blocks[-1]["rows"][0]["cells"]
    assert helped == ["Zezima", "Twisted bow, Dragon bones"]

    # An outsider sees the leaders first (Blue leads on score).
    outsider = pe._task_page(s, _ev(), _detail(), 1, None)
    check = next(b for b in outsider if b["type"] == "table")
    assert [c["label"] for c in check["columns"]][2:] == ["Blue", "Red"]


def test_task_page_paths_and_refusals(monkeypatch):
    import web_api.routes.events as web_events

    bd = {"structure": "paths", "contributors": [], "paths": [
        {"label": "Pet", "groups": [{"mode": "any_of", "need": 1, "obtained": 0,
                                     "items": [_item("Pet general graardor", 0)]}]},
        {"label": "5,000 KC", "groups": [], "need": 5000, "got": 1200, "unit": "KC",
         "npcs": [{"name": "General Graardor"}, {"name": "Kree'arra"}]},
    ]}
    monkeypatch.setattr(web_events, "team_task_breakdown", lambda *a: bd)
    s = _session(SimpleNamespace(id=2), [SimpleNamespace(id=10), SimpleNamespace(id=11)])
    blocks = pe._task_page(s, _ev(), _detail(), 2, None)
    headings = [b["text"] for b in blocks if b["type"] == "heading"]
    assert "Option 1: Pet  ·  Any one of these  (0 / 1)" in headings
    assert "Option 2: 5,000 KC" in headings
    kc = [b for b in blocks if b["type"] == "table"][1]
    assert kc["rows"][0]["cells"] == ["Option 2: 5,000 KC", "5,000 KC", "1,200 / 5,000", "1,200 / 5,000"]
    assert any(b.get("text") == "Counts at: General Graardor, Kree'arra" for b in blocks)

    with pytest.raises(pe._Missing):
        pe._task_page(s, _ev(), _detail(), 3, None)  # managed
    with pytest.raises(pe._Missing):
        pe._task_page(s, _ev(), _detail(tasks_hidden=True), 1, None)
    with pytest.raises(pe._Missing):
        pe._task_page(s, _ev(kind="loot_sweep"), _detail(), 1, None)
