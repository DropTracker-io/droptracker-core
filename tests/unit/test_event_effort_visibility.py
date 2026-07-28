"""Per-event EHE visibility (web74a).

Some clans don't want a per-member effort number on a public page — it reads as
"here is who did the least". The gate is DISPLAY only: effort keeps being
recorded, so flipping the toggle back on later reveals the full history rather
than starting from zero.
"""

from unittest.mock import MagicMock, patch

import pytest

from web_api.routes import events as ev_routes


class _Event:
    def __init__(self, visibility=None):
        if visibility is not None:
            self.effort_visibility = visibility


class TestVisibilityCoercion:
    @pytest.mark.parametrize("raw,expected", [
        ("public", "public"),
        ("admins", "admins"),
        ("ADMINS", "admins"),
        ("  admins  ", "admins"),
    ])
    def test_known_values(self, raw, expected):
        assert ev_routes._effort_visibility_value(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "nonsense", 7, "private"])
    def test_unknown_values_fall_back_to_public(self, raw):
        # Forgiving on purpose: a bad value must not fail an otherwise valid
        # event save, and today's behaviour is the safe landing spot.
        assert ev_routes._effort_visibility_value(raw) == "public"


class TestVisibilityGate:
    def test_public_events_show_everyone(self):
        assert ev_routes._effort_visible(MagicMock(), None, _Event("public")) is True

    def test_missing_column_reads_as_public(self):
        # Pre-migration rows / legacy objects keep working.
        assert ev_routes._effort_visible(MagicMock(), None, _Event()) is True

    def test_admins_only_hides_it_from_anonymous_viewers(self):
        with patch.object(ev_routes, "_is_event_admin", return_value=False):
            assert ev_routes._effort_visible(MagicMock(), None, _Event("admins")) is False

    def test_admins_only_still_shows_the_event_admin(self):
        with patch.object(ev_routes, "_is_event_admin", return_value=True) as gate:
            assert ev_routes._effort_visible(MagicMock(), 42, _Event("admins")) is True
        gate.assert_called_once()

    def test_public_never_pays_for_the_admin_lookup(self):
        # The gate short-circuits: a public event must not run a role check on
        # every anonymous page view.
        with patch.object(ev_routes, "_is_event_admin") as gate:
            ev_routes._effort_visible(MagicMock(), None, _Event("public"))
        gate.assert_not_called()
