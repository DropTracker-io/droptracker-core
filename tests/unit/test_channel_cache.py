"""services/channel_cache.shape_channel_cache — the guild channel cache payload
that backs the website's channel pickers (text, forum, thread, category, voice).

Loaded via importlib (`services/` is not a package — same pattern as
test_event_notifications.py)."""
import importlib.util
import os
from types import SimpleNamespace

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "channel_cache.py"
)
_spec = importlib.util.spec_from_file_location("_channel_cache_under_test", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
shape_channel_cache = _mod.shape_channel_cache


class GuildText(SimpleNamespace):
    pass


class GuildForum(SimpleNamespace):
    pass


class GuildVoice(SimpleNamespace):
    pass


class GuildPublicThread(SimpleNamespace):
    pass


class GuildCategory(SimpleNamespace):
    pass


class GuildStageVoice(SimpleNamespace):
    pass


class GuildNews(SimpleNamespace):
    pass


def _channel(cls, id, name, position):
    return cls(id=id, name=name, position=position)


def _thread(id, name, parent_id):
    return GuildPublicThread(id=id, name=name, parent_id=parent_id)


def test_text_and_forum_channels_typed_and_position_sorted():
    out = shape_channel_cache(
        [
            _channel(GuildText, 2, "general", 1),
            _channel(GuildForum, 3, "achievements", 2),
            _channel(GuildText, 1, "drops", 0),
        ],
        [],
    )
    assert [(c["id"], c["type"]) for c in out] == [
        ("1", "text"),
        ("2", "text"),
        ("3", "forum"),
    ]


def test_voice_channels_are_typed_for_the_stat_display_pickers():
    # Voice channels are not messageable, but the `vc_to_display_*` settings
    # RENAME one every 10 minutes rather than post in it, so they have to be
    # offered. They used to be dropped here, which is why those two settings
    # could only be configured by pasting a raw channel id.
    out = shape_channel_cache([_channel(GuildVoice, 9, "General", 0)], [])
    assert out == [{"id": "9", "name": "General", "position": 0, "type": "voice"}]


def test_stage_channels_count_as_voice():
    out = shape_channel_cache([_channel(GuildStageVoice, 10, "Stage", 0)], [])
    assert [c["type"] for c in out] == ["voice"]


def test_channel_kinds_the_cache_has_no_use_for_are_still_excluded():
    # Announcement channels and DMs have no picker behind them; leaving the
    # fallthrough in place keeps the payload to kinds a picker actually offers.
    out = shape_channel_cache([_channel(GuildNews, 11, "announcements", 0)], [])
    assert out == []


def test_categories_are_typed_for_the_per_team_channel_picker():
    # Categories surface so the website can offer a category target for
    # per-team (permissioned) channels — distinct from forums/text/threads.
    out = shape_channel_cache(
        [
            _channel(GuildCategory, 5, "Teams", 0),
            _channel(GuildText, 6, "general", 1),
        ],
        [],
    )
    assert [(c["id"], c["type"]) for c in out] == [
        ("5", "category"),
        ("6", "text"),
    ]


def test_threads_follow_their_parent_sorted_by_name():
    out = shape_channel_cache(
        [
            _channel(GuildForum, 10, "achievements", 0),
            _channel(GuildText, 20, "general", 1),
        ],
        [
            _thread(12, "pbs", 10),
            _thread(11, "Drops", 10),
            _thread(21, "help-me", 20),
        ],
    )
    assert [(c["id"], c["type"]) for c in out] == [
        ("10", "forum"),
        ("11", "thread"),  # "Drops" sorts before "pbs" case-insensitively
        ("12", "thread"),
        ("20", "text"),
        ("21", "thread"),
    ]
    drops = out[1]
    assert drops["parent_id"] == "10"
    assert drops["position"] == 0  # inherits the parent's position


def test_orphan_threads_are_dropped():
    out = shape_channel_cache(
        [_channel(GuildText, 1, "general", 0)],
        [_thread(99, "under-announcement-channel", 55), _thread(98, "no-parent", None)],
    )
    assert [c["id"] for c in out] == ["1"]


def test_ids_are_strings_and_missing_position_defaults_to_zero():
    out = shape_channel_cache(
        [GuildText(id=123, name="drops", position=None)],
        [],
    )
    assert out == [{"id": "123", "name": "drops", "position": 0, "type": "text"}]
