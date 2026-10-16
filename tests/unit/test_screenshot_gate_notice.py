"""Chat notice for a submission a group dropped for having no screenshot.

The notice reads the player's stored plugin settings to name the setting that
skipped the screenshot (suggestion #168), and must never raise.
"""

import json
from unittest.mock import MagicMock

from data.submissions.common import screenshot_gate_notice


def _session(screenshots=None, raw=None):
    """A session whose PlayerPluginConfig lookup returns the given settings."""
    if raw is None and screenshots is not None:
        raw = json.dumps({"v": 1, "settings": {"Screenshots": screenshots}})
    session = MagicMock()
    session.query.return_value.filter.return_value.scalar.return_value = raw
    return session


class TestScreenshotGateNotice:
    def test_screenshots_switched_off(self):
        notice = screenshot_gate_notice(
            _session({"screenshots": False}), 7, "Realists", "pet"
        )
        assert notice.startswith("Realists only posts submissions with a screenshot, so your pet was not posted.")
        assert "Turn on 'Enable Screenshots'" in notice

    def test_level_below_players_own_minimum(self):
        # The #168 case: clan posts from 50, plugin screenshots from 75, level 55.
        notice = screenshot_gate_notice(
            _session({"screenshots": True, "minLevelToScreenshot": 75}),
            7, "Velayn's clan", "level-up", level=55, group_min_level=50,
        )
        assert "only takes level-up screenshots from level 75" in notice
        assert "'Minimum level to screenshot'" in notice
        assert "Lower it to 50 or lower" in notice

    def test_level_hint_omits_target_when_group_min_unknown(self):
        notice = screenshot_gate_notice(
            _session({"screenshots": True, "minLevelToScreenshot": 75}),
            7, "G", "level-up", level=55,
        )
        assert "Lower it to have these posted." in notice

    def test_level_at_or_above_players_minimum_is_not_blamed_on_it(self):
        notice = screenshot_gate_notice(
            _session({"screenshots": True, "minLevelToScreenshot": 75}),
            7, "G", "level-up", level=80, group_min_level=50,
        )
        assert "Minimum level" not in notice
        assert "Check that screenshots are turned on" in notice

    def test_loot_below_players_screenshot_value(self):
        notice = screenshot_gate_notice(
            _session({"screenshots": True, "screenshotValue": 250000}),
            7, "G", "Abyssal whip drop", value=200000,
        )
        assert "your Abyssal whip drop was not posted" in notice
        assert "only takes loot screenshots above 250,000 gp" in notice

    def test_string_typed_settings_are_read(self):
        notice = screenshot_gate_notice(
            _session({"screenshots": "true", "minLevelToScreenshot": "75"}),
            7, "G", "level-up", level=60, group_min_level=60,
        )
        assert "from level 75" in notice

    def test_no_snapshot_falls_back_to_generic(self):
        notice = screenshot_gate_notice(_session(raw=None), 7, "G", "death")
        assert notice == (
            "G only posts submissions with a screenshot, so your death was not posted. "
            "Check that screenshots are turned on in the DropTracker plugin settings."
        )

    def test_unreadable_snapshot_falls_back_to_generic(self):
        notice = screenshot_gate_notice(_session(raw="{not json"), 7, "G", "quest")
        assert "Check that screenshots are turned on" in notice

    def test_lookup_error_never_raises(self):
        session = MagicMock()
        session.query.side_effect = RuntimeError("db down")
        notice = screenshot_gate_notice(session, 7, "G", "diary")
        assert "Check that screenshots are turned on" in notice

    def test_no_player_id_skips_the_lookup(self):
        session = MagicMock()
        notice = screenshot_gate_notice(session, None, "G", "pet")
        session.query.assert_not_called()
        assert "Check that screenshots are turned on" in notice
