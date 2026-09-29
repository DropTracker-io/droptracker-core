"""Event-wide lootboard: the KC/EHE player panel (lootboard/event_board_panel.py),
row ranking + gates (lootboard/event_boards.py) and Discord delivery
(services/event_lootboard_post.py, loaded by file path — conftest stubs the
``services`` package)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

import pytest
from PIL import Image

from lootboard import event_board_panel as panel
from lootboard import event_boards as eb

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gp(v):
    return f"{v / 1e6:.2f}M"


def _colour(v):
    return (0, 255, 128)


def _template():
    """A stand-in theme: the right size, a plain body and a lighter table."""
    img = Image.new("RGB", (panel.BOARD_W, panel.BOARD_H), (40, 36, 30))
    for y in range(120, 500):
        for x in range(4, 296, 7):
            img.putpixel((x, y), (60, 55, 45))
    return img


def _rows(n, teams=2):
    return [panel.PanelRow(rank=i + 1, name=f"Player {i}", team=f"Team {i % teams}",
                           team_color=(200, 50, 50), kc=i * 3, ehe=float(n - i),
                           loot=1_000_000 * i) for i in range(n)]


class TestPanel:
    def test_column_widths_fill_the_table(self):
        cols = panel.default_columns(True, True, 1020)
        widths = panel.column_widths(cols, 1020)
        assert sum(widths) + panel.chrome_width(len(cols)) == 1020
        assert all(w >= c.min_w for w, c in zip(widths, cols))

    def test_team_names_only_when_they_fit(self):
        # Full width: names. Half width with KC/EHE: swatch only. Half width
        # loot-only: there is room for names again.
        assert panel.team_names_fit(True, True, 1020)
        assert not panel.team_names_fit(True, True, 505)
        assert panel.team_names_fit(True, False, 505)
        assert not panel.team_names_fit(False, False, 1020)

    def test_compose_splices_panel_above_footer(self):
        tpl = _template()
        spec = panel.PanelSpec(title="Players This Event", rows=_rows(6),
                               note=eb.EFFORT_NOTE, teams=[("Team 0", (1, 2, 3))])
        out = panel.compose(tpl.copy(), tpl, spec, _gp, _colour)
        assert out.width == panel.BOARD_W
        assert out.height > panel.BOARD_H
        # Footer is moved, not overwritten: the bottom rows are the template's.
        extra = out.height - panel.BOARD_H
        assert out.crop((0, panel.SPLIT_Y + extra, out.width, out.height)).tobytes() == \
            tpl.crop((0, panel.SPLIT_Y, tpl.width, tpl.height)).tobytes()

    def test_many_rows_split_into_two_tables_and_cap(self):
        tpl = _template()
        few = panel.compose(tpl.copy(), tpl, panel.PanelSpec("t", _rows(14)), _gp, _colour)
        many = panel.compose(tpl.copy(), tpl, panel.PanelSpec("t", _rows(28)), _gp, _colour)
        # 28 rows side by side are no taller than 14 stacked.
        assert many.height == few.height
        capped = panel.compose(tpl.copy(), tpl,
                               panel.PanelSpec("t", _rows(80), more=40), _gp, _colour)
        assert capped.height < panel.BOARD_H + 40 * panel.ROW_H

    def test_formatting(self):
        assert panel.format_hours(None) == "-"
        assert panel.format_hours(0) == "0h"
        assert panel.format_hours(12.34) == "12.3h"
        assert panel.format_hours(5.0, estimated=True) == "~5.0h"
        assert panel.format_hours(250.0) == "250h"
        assert panel.format_count(None) == "-"
        assert panel.format_count(1234) == "1,234"
        assert panel.format_count(250_000) == "250K"


class TestRows:
    names = {1: "Alpha", 2: "bravo", 3: "Charlie", 0: "Zero"}
    team_of = {1: 10, 2: 10, 3: 20, 0: 20}
    teams = {10: ("Reds", (255, 0, 0)), 20: ("Blues", (0, 0, 255))}
    loot = {1: 500, 2: 900, 3: 100, 0: 50}
    effort = {1: {"kills": 10, "hours": 3.0, "estimated": False},
              3: {"kills": 40, "hours": 9.5, "estimated": True},
              0: {"kills": 1, "hours": 0.2, "estimated": False}}

    def test_ranked_by_ehe_when_effort_shows(self):
        rows = eb.build_rows([1, 2, 3, 0], self.names, self.team_of, self.teams,
                             self.loot, self.effort, show_effort=True)
        assert [r.name for r in rows] == ["Charlie", "Alpha", "Zero", "bravo"]
        assert [r.rank for r in rows] == [1, 2, 3, 4]
        assert rows[0].ehe_estimated and rows[0].team == "Blues"
        assert rows[3].kc == 0 and rows[3].ehe == 0.0

    def test_ranked_by_loot_without_effort(self):
        rows = eb.build_rows([1, 2, 3, 0], self.names, self.team_of, self.teams,
                             self.loot, self.effort, show_effort=False)
        assert [r.name for r in rows] == ["bravo", "Alpha", "Charlie", "Zero"]
        assert all(r.kc is None and r.ehe is None for r in rows)

    def test_player_id_zero_is_a_real_player(self):
        rows = eb.build_rows([0], self.names, self.team_of, self.teams,
                             self.loot, self.effort, show_effort=True)
        assert rows[0].name == "Zero" and rows[0].kc == 1

    def test_cold_rate_cache_shows_no_hours(self):
        rows = eb.build_rows([1], self.names, self.team_of, self.teams,
                             self.loot, self.effort, show_effort=True, rates_known=False)
        assert rows[0].ehe is None and rows[0].kc == 10


class TestGates:
    def test_flag_follows_team_flag_unless_set(self, monkeypatch):
        monkeypatch.delenv(eb.FEATURE_FLAG_ENV, raising=False)
        monkeypatch.setenv(eb.TEAM_FLAG_ENV, "true")
        assert eb.feature_enabled()
        monkeypatch.setenv(eb.FEATURE_FLAG_ENV, "0")
        assert not eb.feature_enabled()
        monkeypatch.setenv(eb.FEATURE_FLAG_ENV, "1")
        monkeypatch.setenv(eb.TEAM_FLAG_ENV, "")
        assert eb.feature_enabled()

    def test_final_render_after_end(self, tmp_path):
        path = tmp_path / "lootboard.png"
        ended = types.SimpleNamespace(status="past", ended_at=datetime.now())
        assert eb._due(ended, str(path))            # never rendered
        path.write_bytes(b"x")
        old = (datetime.now() - timedelta(hours=2)).timestamp()
        os.utime(path, (old, old))
        assert eb._due(ended, str(path))            # rendered before the end
        os.utime(path, None)
        later = types.SimpleNamespace(status="past",
                                      ended_at=datetime.now() - timedelta(minutes=5))
        assert not eb._due(later, str(path))        # final render done

    def test_hourly_while_running(self, tmp_path):
        path = tmp_path / "lootboard.png"
        live = types.SimpleNamespace(status="active", ended_at=None)
        assert eb._due(live, str(path))
        path.write_bytes(b"x")
        assert not eb._due(live, str(path))

    def test_hex_colours(self):
        assert eb._hex_rgb("#ff8000") == (255, 128, 0)
        assert eb._hex_rgb(None) is None
        assert eb._hex_rgb("#zzzzzz") is None


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def _load_post_module():
    path = os.path.join(_ROOT, "services", "event_lootboard_post.py")
    spec = importlib.util.spec_from_file_location("_event_lootboard_post_ut", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRedis:
    def __init__(self):
        self.data = {}

    def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.data.get(key, {}).items()}

    def hset(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)

    def expire(self, key, ttl):
        pass

    def delete(self, key):
        self.data.pop(key, None)


class _Msg:
    def __init__(self, mid):
        self.id = mid
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        m = _Msg(1000 + len(self.sent))
        self.sent.append(m)
        return m


@pytest.fixture
def post(monkeypatch, tmp_path):
    mod = _load_post_module()
    helpers = types.SimpleNamespace(
        _EDIT_MISSING="missing", _EDIT_UNAVAILABLE="unavailable",
        edits=[], deletes=[],
    )

    async def _edit(channel, message_id, components, files):
        helpers.edits.append(message_id)
        return None, "edited"

    async def _delete(channel, message_id):
        helpers.deletes.append(message_id)

    async def _repost(channel, stale, components, files):
        if stale:
            await _delete(channel, stale)
        return await channel.send(components=components, files=files)

    def _read(path):
        with open(path, "rb") as f:
            return f.read(), os.path.getmtime(path)

    helpers._edit_tracked_message = _edit
    helpers._delete_bot_message = _delete
    helpers._repost_tracked_message = _repost
    helpers._read_png = _read
    monkeypatch.setitem(sys.modules, "services.event_team_discord_bot", helpers)
    monkeypatch.setattr(mod, "_payload", lambda event, png: ("components", "file"))
    conn = _FakeRedis()
    channel = _Channel()

    async def fetch_channel(cid):
        return channel

    bot = types.SimpleNamespace(fetch_channel=fetch_channel)
    png = tmp_path / "lootboard.png"
    png.write_bytes(b"png-1")
    return types.SimpleNamespace(mod=mod, helpers=helpers, conn=conn, channel=channel,
                                 bot=bot, path=str(png))


def _row(board_message_id="500"):
    return types.SimpleNamespace(id=7, channel_id="42", message_id=board_message_id)


def test_posts_once_then_skips_unchanged(post):
    ev = types.SimpleNamespace(id=1, name="Bingo")
    run = lambda: asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(),
                                                    post.path, "Bingo"))
    assert run() is True
    assert len(post.channel.sent) == 1
    state = post.mod._state(post.conn, 7)
    assert state["message_id"] == "1000"
    # Same file on disk: no Discord call at all.
    assert run() is False
    assert post.helpers.edits == []


def test_new_image_edits_in_place(post):
    ev = types.SimpleNamespace(id=1, name="Bingo")
    asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(), post.path, "Bingo"))
    with open(post.path, "wb") as f:
        f.write(b"png-2")
    later = os.path.getmtime(post.path) + 60
    os.utime(post.path, (later, later))
    assert asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(),
                                             post.path, "Bingo")) is True
    assert post.helpers.edits == ["1000"]
    assert len(post.channel.sent) == 1


def test_board_reposted_above_us_moves_ours_below(post):
    ev = types.SimpleNamespace(id=1, name="Bingo")
    asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(), post.path, "Bingo"))
    # The board post was re-created with a newer id than our lootboard.
    asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row("5000"),
                                      post.path, "Bingo"))
    assert post.helpers.deletes == ["1000"]
    assert post.mod._state(post.conn, 7)["message_id"] == "1001"


def test_missing_image_is_silent(post):
    ev = types.SimpleNamespace(id=1, name="Bingo")
    os.remove(post.path)
    assert asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(),
                                             post.path, "Bingo")) is False
    assert post.channel.sent == []


def test_turning_it_off_retires_the_message(post):
    ev = types.SimpleNamespace(id=1, name="Bingo")
    asyncio.run(post.mod._refresh_row(post.bot, post.conn, ev, _row(), post.path, "Bingo"))
    state = post.mod._state(post.conn, 7)
    assert asyncio.run(post.mod._retire(post.bot, post.conn, _row(), state)) is True
    assert post.helpers.deletes == ["1000"]
    assert post.mod._state(post.conn, 7) == {}


def test_below_never_guesses():
    mod = _load_post_module()
    assert mod._below("20", "10")
    assert not mod._below("10", "20")
    assert mod._below(None, "10")
