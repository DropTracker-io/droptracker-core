"""Slayer task notifications: which groups are told, and what they are sent.

Completions have been recorded since plugin 6.0.6. These tests cover the
announcement half added on top: the ``notify_slayer_tasks`` /
``slayer_excluded_masters`` / ``channel_id_to_post_slayer`` settings, the
enqueue loop in data/submissions/slayer.py, and the three shapes a group can be
sent (the code-built embed, its template twin in the embed builder, and the
Components V2 default).

notification_service is loaded from its file path, like
test_notification_default_embeds.py, because conftest stubs ``services``.
"""

import importlib
import importlib.util
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.local_artifacts import ts_registry
from utils.slayer_masters import (
    DEFAULT_EXCLUDED_MASTER_IDS,
    SLAYER_MASTERS,
    excluded_master_ids_from_config,
    master_by_id,
)
from web_api import config_registry as reg

for _name in ("services.contribution_notifications", "services.event_notifications"):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_slayer_notifications_under_test",
    os.path.join(_REPO, "services", "notification_service.py"),
)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_slayer_notifications_under_test"] = ns
_spec.loader.exec_module(ns)

cl = importlib.import_module("services.component_layout")

TURAEL, DURADEL, SPRIA, MORTIMER = 1, 5, 9, 10

# A real completion from prod (row 3028), as the sender receives it.
FULL = {
    "group_id": 303,
    "player_name": "Zezima",
    "player_id": 5,
    "task_name": "Abyssal Demons",
    "task_id": 42,
    "master_id": DURADEL,
    "master_name": "Duradel",
    "amount_initial": 206,
    "amount_killed": 206,
    "streak": 377,
    "points_awarded": 15,
    "points_total": 580,
    "xp_gained": 139468,
}
EXPECTED_LINK = "[Zezima](https://www.droptracker.io/players/5)"


# ── Which masters a group skips ──────────────────────────────────────────────

class TestExcludedMasters:
    def test_a_group_that_never_chose_skips_the_reset_masters(self):
        assert excluded_master_ids_from_config(None) == {TURAEL, SPRIA}
        assert excluded_master_ids_from_config(None) == DEFAULT_EXCLUDED_MASTER_IDS

    def test_a_saved_empty_value_skips_nobody(self):
        # Distinct from "never saved": the admin unticked every box.
        assert excluded_master_ids_from_config("") == frozenset()

    def test_the_stored_form_is_read_as_ids(self):
        assert excluded_master_ids_from_config("1,9") == {TURAEL, SPRIA}
        assert excluded_master_ids_from_config("10") == {MORTIMER}
        assert excluded_master_ids_from_config(" 5 , 10 ") == {DURADEL, MORTIMER}

    def test_junk_is_ignored_rather_than_costing_the_notification(self):
        assert excluded_master_ids_from_config("1,x,99,,Turael") == {TURAEL}

    def test_labels_name_the_alternates_but_not_a_longer_spelling(self):
        assert master_by_id(TURAEL).label == "Turael / Aya"
        assert master_by_id(DURADEL).label == "Duradel / Kuradal"
        assert master_by_id(8).label == "Konar"


class TestRegistry:
    def test_the_settings_exist_with_opt_in_defaults(self):
        notify = reg.get_config_field("notify_slayer_tasks")
        assert notify["type"] == "boolean" and notify["default"] is False
        assert reg.get_config_field("seasonal_notify_slayer_tasks") is notify
        assert reg.get_config_field("channel_id_to_post_slayer")["type"] == "channel"

    def test_the_master_picker_offers_every_master_and_defaults_to_the_reset_ones(self):
        field = reg.get_config_field("slayer_excluded_masters")
        assert field["type"] == "multiselect"
        assert [o["value"] for o in field["options"]] == [str(m.id) for m in SLAYER_MASTERS]
        assert excluded_master_ids_from_config(field["default"]) == DEFAULT_EXCLUDED_MASTER_IDS
        # An absent row reads back as the default, which is what the web shows.
        assert reg.coerce_from_storage(field, None) == field["default"]

    @pytest.mark.parametrize("value, stored", [
        (["9", "1"], "1,9"),
        ("9,1", "1,9"),
        ("10, 5", "5,10"),
        ([], ""),
        ("", ""),
        (None, ""),
        (["1", "1"], "1"),
    ])
    def test_coerce_writes_option_order(self, value, stored):
        assert reg.coerce_to_storage("slayer_excluded_masters", value) == stored

    @pytest.mark.parametrize("value", [["11"], "Turael", "1,42", True, {"1": True}])
    def test_coerce_rejects_what_is_not_a_master(self, value):
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("slayer_excluded_masters", value)

    @pytest.mark.skipif(ts_registry("group-config.ts") is None,
                        reason="web repo not checked out beside this one")
    def test_the_web_picker_lists_the_same_masters(self):
        src = open(ts_registry("group-config.ts"), encoding="utf-8").read()
        block = re.search(r"SLAYER_MASTER_OPTIONS[^=]*=\s*\[(.*?)\];", src, re.DOTALL)
        assert block, "SLAYER_MASTER_OPTIONS not found in group-config.ts"
        ts = re.findall(r'value:\s*"(\d+)",\s*label:\s*"([^"]+)"', block.group(1))
        assert ts == [(str(m.id), m.label) for m in SLAYER_MASTERS]
        assert re.search(r'key:\s*"slayer_excluded_masters"[^}]*default:\s*"1,9"', src, re.DOTALL)


# ── Who gets a notification ──────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, notification_type, player_id, data, group_id=None, existing_session=None):
        self.calls.append((notification_type, player_id, data, group_id))


@pytest.fixture
def enqueue(monkeypatch):
    """Runs the enqueue loop against in-memory group config."""
    from data.submissions import slayer
    from utils import group_config as gc

    config = {}
    screenshot_groups = set()
    recorder = _Recorder()
    groups = [SimpleNamespace(group_id=2, group_name="DropTracker"),
              SimpleNamespace(group_id=303, group_name="Clan")]

    monkeypatch.setattr(gc, "get", lambda s, gid, key, default=None: config.get((gid, key), default))
    monkeypatch.setattr(slayer, "get_player_groups_with_global", lambda s, p: groups)
    monkeypatch.setattr(slayer, "create_notification", recorder)

    async def required(session, group_id):
        return group_id in screenshot_groups

    monkeypatch.setattr(slayer, "screenshot_required", required)

    async def run(master_id=DURADEL, image_url="https://x/s.png", config_prefix=""):
        entry = SimpleNamespace(
            id=77, player_id=5, task_name="Abyssal Demons", task_id=42, boss_id=None,
            master_id=master_id, master_name=master_by_id(master_id).name if master_by_id(master_id) else None,
            amount_initial=206, amount_killed=206, streak=377, points_awarded=15,
            points_total=580, xp_gained=139468, timestamp=1790164749,
        )
        notice = await slayer._queue_group_notifications(
            MagicMock(), SimpleNamespace(player_id=5), entry,
            player_name="Zezima", config_prefix=config_prefix, unique_id="guid-1",
            image_url=image_url, video_key=None, video_url=None, world_type="main",
            plugin_version="6.0.9", use_external_session=True,
        )
        return notice, recorder.calls

    return SimpleNamespace(run=run, config=config, screenshot_groups=screenshot_groups)


class TestWhoIsTold:
    async def test_nobody_is_told_until_a_group_turns_it_on(self, enqueue):
        _, calls = await enqueue.run()
        assert calls == []

    async def test_a_group_with_it_on_is_sent_the_task(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        _, calls = await enqueue.run()
        assert [(t, gid) for t, _, _, gid in calls] == [("slayer", 303)]
        data = calls[0][2]
        assert data["group_id"] == 303
        assert data["task_name"] == "Abyssal Demons"
        assert data["master_name"] == "Duradel"
        assert data["amount_killed"] == 206 and data["points_awarded"] == 15
        assert data["slayer_id"] == 77 and data["guid"] == "guid-1"
        assert data["plugin_version"] == "6.0.9"

    async def test_turael_and_spria_are_skipped_by_default(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        for master in (TURAEL, SPRIA):
            _, calls = await enqueue.run(master_id=master)
            assert calls == [], master

    async def test_a_group_can_choose_to_hear_about_every_master(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        enqueue.config[(303, "slayer_excluded_masters")] = ""
        _, calls = await enqueue.run(master_id=TURAEL)
        assert len(calls) == 1

    async def test_a_group_can_skip_a_real_master(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        enqueue.config[(303, "slayer_excluded_masters")] = "10"
        _, skipped = await enqueue.run(master_id=MORTIMER)
        assert skipped == []
        _, calls = await enqueue.run(master_id=TURAEL)
        assert len(calls) == 1, "choosing its own list replaces the default"

    async def test_an_unknown_master_is_never_skipped(self, enqueue):
        # Leagues' master (id 11) is unnamed; no list can name it.
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        _, calls = await enqueue.run(master_id=11)
        assert len(calls) == 1

    async def test_a_screenshot_requirement_holds_it_back_and_says_why(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        enqueue.screenshot_groups.add(303)
        notice, calls = await enqueue.run(image_url="")
        assert calls == []
        assert "screenshot" in notice and "Clan" in notice

    async def test_seasonal_completions_read_the_seasonal_switch(self, enqueue):
        enqueue.config[(303, "notify_slayer_tasks")] = "1"
        _, calls = await enqueue.run(config_prefix="seasonal_")
        assert calls == [], "the main-world switch says nothing about Leagues"
        enqueue.config[(303, "seasonal_notify_slayer_tasks")] = "1"
        _, calls = await enqueue.run(config_prefix="seasonal_")
        assert len(calls) == 1

    def test_the_channel_gate_uses_the_slayer_channel_then_drops(self):
        common = pytest.importorskip("data.submissions.common")
        assert common.GROUP_CHANNEL_NOTIFICATION_KEYS["slayer"] == (
            "channel_id_to_post_slayer", "channel_id_to_post_loot",
        )


# ── What they are sent ───────────────────────────────────────────────────────

class TestPlaceholders:
    def test_figures_are_formatted_and_named_by_the_registry(self):
        values = ns.NotificationService._slayer_placeholder_map(FULL)
        assert values["{slayer_task}"] == "Abyssal Demons"
        assert values["{slayer_master}"] == "Duradel"
        assert values["{slayer_kills}"] == "206"
        assert values["{slayer_streak}"] == "377"
        assert values["{slayer_points}"] == "15"
        assert values["{slayer_points_total}"] == "580"
        assert values["{slayer_xp}"] == "139,468"
        assert values["{slayer_icon}"] == ns.SLAYER_ICON_URL

    def test_no_points_and_unknown_figures_resolve_empty(self):
        # Turael/Spria say "0"; the first four of a streak and most Mortimer
        # tasks name no points at all.
        for points in (0, None, ""):
            values = ns.NotificationService._slayer_placeholder_map(
                {**FULL, "points_awarded": points, "master_name": None, "xp_gained": None})
            assert values["{slayer_points}"] == ""
            assert values["{slayer_master}"] == ""
            assert values["{slayer_xp}"] == ""
        assert values["{slayer_kills}"] == "206"

    def test_every_token_the_editor_offers_is_filled(self):
        offered = {f"{{{d['token']}}}" for d in cl.tokens_for("slayer")}
        values = ns.NotificationService._slayer_placeholder_map(FULL)
        slayer_tokens = {t for t in offered if t.startswith("{slayer_")}
        assert slayer_tokens == set(values)


class TestDefaultEmbed:
    @pytest.fixture
    def built(self, monkeypatch):
        calls = []

        def record(*args, **kwargs):
            embed = MagicMock()
            calls.append((kwargs, embed))
            return embed

        monkeypatch.setattr(ns.interactions, "Embed", record)
        service = ns.NotificationService(MagicMock(), MagicMock())

        def build(data):
            service._build_default_slayer_embed(data, "Zezima", 5)
            return calls[-1]

        return build

    def test_the_builder_and_the_editor_template_agree(self, built):
        from web_api.routes.notification_defaults import BUILTIN_EMBEDS

        kwargs, embed = built(FULL)
        template = BUILTIN_EMBEDS["slayer"]
        values = {"{player_name}": EXPECTED_LINK,
                  **ns.NotificationService._slayer_placeholder_map(FULL)}

        def fill(text):
            for token, value in values.items():
                text = text.replace(token, value)
            return text

        assert template["title"] == kwargs["title"]
        assert template["color"].lower() == kwargs["color"].lower()
        assert fill(template["description"]) == kwargs["description"]
        assert fill(template["thumbnail"]) == embed.set_thumbnail.call_args.args[0]
        names = [c.kwargs["name"] for c in embed.add_field.call_args_list]
        values_sent = [c.kwargs["value"] for c in embed.add_field.call_args_list]
        # Every template field but the video link (no video here).
        expected = [f for f in template["fields"] if f["name"] != "Video"]
        assert names == [f["name"] for f in expected]
        assert values_sent == [fill(f["value"]) for f in expected]

    def test_missing_figures_drop_their_fields(self, built):
        kwargs, embed = built({**FULL, "points_awarded": 0, "master_name": None})
        names = [c.kwargs["name"] for c in embed.add_field.call_args_list]
        assert names == ["Task streak", "Slayer XP"]
        assert kwargs["description"] == f"{EXPECTED_LINK} killed **206 Abyssal Demons**."

    def test_the_type_is_in_the_embed_builder(self):
        from web_api.routes import embeds

        assert "slayer" in embeds.EMBED_TYPES
        # A group's editor opens on the built-in design, not a blank form.
        default = embeds._builtin_default("slayer")
        assert default["title"] == "Slayer Task Completed"
        assert embeds._builtin_default("drop") is None


class TestComponentsDefault:
    def test_it_is_a_customisable_type_with_a_default(self):
        assert "slayer" in cl.NOTIFICATION_TYPES
        layout = cl.default_layout("slayer")
        assert layout["accent_color"] == ns.SLAYER_COLOR
        first = layout["blocks"][0]
        assert first["content"] == "**{player_name}** completed a slayer task!"

    def test_a_task_with_no_points_or_master_loses_only_those_lines(self):
        values = {"{player_name}": "Zezima",
                  **ns.NotificationService._slayer_placeholder_map(
                      {**FULL, "points_awarded": None, "master_name": None}),
                  "{image_url}": ""}
        payload = cl.render_layout(cl.default_layout("slayer"), values)
        text = str(payload)
        assert "206 Abyssal Demons" in text
        assert "Task streak" in text and "Slayer XP" in text
        assert "Points earned" not in text and "**Master**" not in text
        assert ns.SLAYER_ICON_URL in text
