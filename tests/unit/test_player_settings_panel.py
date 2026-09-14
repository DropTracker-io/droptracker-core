"""Unit tests for the pure selection→storage mapping of the /settings panel
(services/player_settings_panel.py). Storage semantics must mirror the
website settings API (web_api/routes/me.py) exactly."""

import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest stubs `interactions` as a flat MagicMock (not a package), so the
# module's `from interactions.api.events import ...` needs the subpath stubbed.
from unittest.mock import MagicMock  # noqa: E402

for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())

_spec = importlib.util.spec_from_file_location(
    "_player_settings_panel_ut",
    os.path.join(_ROOT, "services", "player_settings_panel.py"))
panel = importlib.util.module_from_spec(_spec)
sys.modules["_player_settings_panel_ut"] = panel
_spec.loader.exec_module(panel)


def test_dm_selection_writes_every_key_explicitly():
    updates = panel.dm_updates_from_selection({"dm_drops", "dm_monthly_recap"})
    assert updates["dm_drops"] == "true"
    assert updates["dm_monthly_recap"] == "true"
    # Unselected keys are explicit "false" rows — including the default-on
    # opt-out key, whose absent-row state means enabled.
    assert updates["dm_clan_invites"] == "false"
    assert updates["dm_account_changes"] == "false"
    assert set(updates) == {k for k, *_ in panel.DM_OPTIONS}


def test_event_prefs_persist_disabled_only():
    types = ("event_completion", "event_line", "event_started")
    raw = panel.event_prefs_from_selection({"event_completion"}, types)
    assert json.loads(raw) == {"event_line": False, "event_started": False}
    # Everything selected -> empty object, so future types default on.
    assert json.loads(panel.event_prefs_from_selection(set(types), types)) == {}


def test_event_pref_labels_cover_all_types():
    # The panel's label table must keep up with WEB_PREF_TYPES (a new type
    # would otherwise render as its raw key).
    spec = importlib.util.spec_from_file_location(
        "_plugin_notifications_labels_ut",
        os.path.join(_ROOT, "services", "plugin_notifications.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.WEB_PREF_TYPES) == set(panel.EVENT_PREF_LABELS)


# ── Death message (members' own death messages) ─────────────────────────────

def test_modal_boxes_come_back_in_order_with_blanks():
    responses = {"m1": "  {player_name} planked ", "m3": "third", "m5": ""}
    assert panel.death_messages_from_modal(responses) == [
        "  {player_name} planked ", "", "third", "", "",
    ]
    assert panel.death_messages_from_modal(None) == ["", "", "", "", ""]


def test_modal_boxes_feed_the_shared_rules():
    # Blank boxes are dropped and names lowercased by the one rule every
    # surface saves through, not by the panel.
    mm = sys.modules["db.member_messages"]
    boxes = panel.death_messages_from_modal({"m1": " {Killer} got me ", "m2": "", "m4": "{killer} got me"})
    assert mm.normalize_messages(boxes) == ["{killer} got me"]


def test_messages_display_as_written_and_cannot_break_out():
    assert panel.display_death_message("{player_name} **died**") == "`{player_name} **died**`"
    assert panel.display_death_message("a ` b") == "`a ' b`"


def test_group_line_says_where_it_posts_and_why_not():
    text = panel.describe_death_message_groups([
        {"id": 1, "name": "Posting Clan", "allowed": True, "blocked": False},
        {"id": 2, "name": "Quiet Clan", "allowed": False, "blocked": False},
        {"id": 3, "name": "Strict Clan", "allowed": True, "blocked": True},
    ])
    assert text == "**Posting Clan** · Quiet Clan (not switched on) · Strict Clan (blocked by its leaders)"
    assert panel.describe_death_message_groups([]) == "no clans yet"


def test_group_line_stays_short_for_members_of_many_clans():
    groups = [{"id": i, "name": f"Clan {i}", "allowed": False, "blocked": False} for i in range(20)]
    text = panel.describe_death_message_groups(groups)
    assert text.endswith("and 5 more")
    assert "Clan 15" not in text


def test_panel_lists_messages_and_groups(monkeypatch):
    monkeypatch.setattr(panel, "_players", lambda uid: [{"id": 9, "name": "Iron Ron"}])
    monkeypatch.setattr(panel, "_death_message_state", lambda uid, pid: {
        "name": "Iron Ron",
        "messages": ["{player_name} forgot to pray against {killer}", "{player_name} planked"],
        "groups": [{"id": 5, "name": "Clan", "allowed": True, "blocked": False}],
    })
    content, components = panel.build_death_message_panel(7)
    assert "## 💀 Death message — `Iron Ron`" in content
    assert "1. `{player_name} forgot to pray against {killer}`" in content
    assert "2. `{player_name} planked`" in content
    assert "**Posted in:** **Clan**" in content
    assert len(content) < 2000
    assert len(components) == 1


def test_panel_with_nothing_written_says_the_clans_use_their_own(monkeypatch):
    monkeypatch.setattr(panel, "_players", lambda uid: [{"id": 9, "name": "Iron Ron"}])
    monkeypatch.setattr(panel, "_death_message_state", lambda uid, pid: {
        "name": "Iron Ron", "messages": [], "groups": [],
    })
    content, _ = panel.build_death_message_panel(7)
    assert "No messages yet" in content
    assert "**Posted in:** no clans yet" in content


def test_panel_asks_which_account_first(monkeypatch):
    monkeypatch.setattr(panel, "_players", lambda uid: [
        {"id": 9, "name": "Iron Ron"}, {"id": 10, "name": "Main Ron"},
    ])
    content, components = panel.build_death_message_panel(7)
    assert "Which account" in content
    assert len(components) == 2


def test_panel_without_accounts_points_at_claiming(monkeypatch):
    monkeypatch.setattr(panel, "_players", lambda uid: [])
    content, _ = panel.build_death_message_panel(7)
    assert "/claim-rsn" in content


def test_modal_prefills_saved_messages_only(monkeypatch):
    calls = []
    monkeypatch.setattr(panel, "ShortText", lambda **kw: calls.append(kw) or kw)
    modal_calls = []
    monkeypatch.setattr(panel, "Modal", lambda *boxes, **kw: modal_calls.append((boxes, kw)) or kw)
    panel.build_death_message_modal(9, ["{player_name} planked", "second"])
    assert [c["custom_id"] for c in calls] == ["m1", "m2", "m3", "m4", "m5"]
    assert calls[0]["value"] == "{player_name} planked"
    assert calls[1]["value"] == "second"
    # No value key at all for an empty box: Discord refuses an empty string.
    assert "value" not in calls[2]
    assert all(c["required"] is False and c["max_length"] == 150 for c in calls)
    boxes, kw = modal_calls[0]
    assert len(boxes) == 5
    assert kw["custom_id"] == "pset:dmsgsave:9"


def _modal_ctx(responses, custom_id="pset:dmsgsave:9"):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    return SimpleNamespace(custom_id=custom_id, author=SimpleNamespace(id=123),
                           responses=responses, send=AsyncMock())


async def test_modal_save_shows_the_rule_it_broke(monkeypatch):
    monkeypatch.setattr(panel, "_resolve_user_id", lambda discord_id: 7)
    seen = {}

    def _save(uid, pid, messages):
        seen.update(uid=uid, pid=pid, messages=messages)
        return None, "Messages can't contain links."

    monkeypatch.setattr(panel, "_save_death_messages", _save)
    ctx = _modal_ctx({"m1": "gp at www.scam.com"})
    await panel.handle_death_message_modal(ctx)
    assert seen == {"uid": 7, "pid": 9, "messages": ["gp at www.scam.com", "", "", "", ""]}
    text = ctx.send.await_args.args[0]
    assert "Not saved:" in text and "Messages can't contain links." in text
    assert ctx.send.await_args.kwargs["ephemeral"] is True


async def test_modal_save_shows_the_updated_panel(monkeypatch):
    monkeypatch.setattr(panel, "_resolve_user_id", lambda discord_id: 7)
    monkeypatch.setattr(panel, "_save_death_messages", lambda uid, pid, m: (["{player_name} planked"], None))
    monkeypatch.setattr(panel, "_players", lambda uid: [{"id": 9, "name": "Iron Ron"}])
    monkeypatch.setattr(panel, "_death_message_state", lambda uid, pid: {
        "name": "Iron Ron", "messages": ["{player_name} planked"], "groups": [],
    })
    ctx = _modal_ctx({"m1": "{player_name} planked"})
    await panel.handle_death_message_modal(ctx)
    text = ctx.send.await_args.args[0]
    assert text.startswith(panel.SAVED)
    assert "1. `{player_name} planked`" in text


async def test_modal_for_someone_elses_account_is_refused(monkeypatch):
    monkeypatch.setattr(panel, "_resolve_user_id", lambda discord_id: 7)
    monkeypatch.setattr(panel, "_save_death_messages", lambda uid, pid, m: (None, "That account isn't linked to you."))
    ctx = _modal_ctx({"m1": "hi"}, custom_id="pset:dmsgsave:99")
    await panel.handle_death_message_modal(ctx)
    assert "That account isn't linked to you." in ctx.send.await_args.args[0]


async def test_a_malformed_modal_id_is_ignored(monkeypatch):
    monkeypatch.setattr(panel, "_resolve_user_id", lambda discord_id: 7)
    ctx = _modal_ctx({}, custom_id="pset:dmsgsave:not-a-number")
    await panel.handle_death_message_modal(ctx)
    ctx.send.assert_not_awaited()


def test_real_storage_refuses_an_account_that_is_not_yours(monkeypatch):
    # _save_death_messages checks ownership before the shared rules run.
    from unittest.mock import MagicMock

    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    monkeypatch.setattr(panel, "Session", lambda: session)
    saved, error = panel._save_death_messages(7, 9, ["{player_name} planked"])
    assert saved is None and error == "That account isn't linked to you."
    session.commit.assert_not_called()
    session.close.assert_called_once()
