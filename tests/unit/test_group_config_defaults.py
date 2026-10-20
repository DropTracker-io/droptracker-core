"""An absent group config row reads as the registry default everywhere (ticket 495).

The website shows the registry default for a group with no row; the
processors and the plugin's /load_config must act on the same value.
"""
from __future__ import annotations

import pytest

from utils import group_config as gc
from web_api.config_registry import GROUP_CONFIG_FIELDS, get_config_field


def _storage_form(default):
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


@pytest.mark.parametrize("key", sorted(gc.ABSENT_DEFAULTS))
def test_absent_defaults_match_registry(key):
    field = get_config_field(key)
    assert field is not None, f"{key} is not a registry key"
    assert gc.ABSENT_DEFAULTS[key] == _storage_form(field["default"])


def test_every_notify_toggle_that_defaults_on_is_covered():
    # The four the ticket was about: shown ON by the site, read OFF elsewhere.
    for key in ("notify_pbs", "notify_clogs", "notify_cas", "notify_pets",
                "min_ca_tier_to_notify"):
        assert key in gc.ABSENT_DEFAULTS


def test_seasonal_mirror_uses_base_default():
    assert gc.absent_default("seasonal_notify_pbs") == "true"
    assert gc.absent_default("seasonal_min_ca_tier_to_notify") == "EASY"
    assert gc.absent_default("lootboard_channel_id") is None


def test_get_effective_fills_only_absent(monkeypatch):
    stored = {(5, "notify_pbs"): "false"}
    monkeypatch.setattr(gc, "get", lambda s, gid, key, default=None: stored.get((gid, key), default))
    assert gc.get_effective(None, 5, "notify_pbs") == "false"   # explicit off wins
    assert gc.get_effective(None, 6, "notify_pbs") == "true"    # absent -> default
    assert gc.get_effective(None, 6, "lootboard_channel_id") is None


def test_get_bulk_effective(monkeypatch):
    monkeypatch.setattr(gc, "get_bulk", lambda s, gids, keys: {(0, "notify_clogs"): "0"})
    out = gc.get_bulk_effective(None, [0, 255], ["notify_clogs", "lootboard_channel_id"])
    assert out[(0, "notify_clogs")] == "0"          # group 0 is a real id
    assert gc.is_truthy(out[(255, "notify_clogs")])
    assert (255, "lootboard_channel_id") not in out


def test_registry_booleans_defaulting_on_are_known():
    """A new default-ON notify toggle must decide its absent reading here too."""
    known_elsewhere = {
        # Readers pass their own matching default or treat '' as on.
        "notify_special_quests", "notify_first_kc", "rank_milestone_bosses",
        "rank_milestone_skills", "rank_milestone_clues", "use_dynamic_colors",
        "use_gp_colors", "clan_broadcast_notify_without_images",
        "clan_chat_relay_muted", "event_wom_reconciliation",
        "bank_show_on_profile", "bank_show_donors",
    }
    on = {f["key"] for f in GROUP_CONFIG_FIELDS
          if f["type"] == "boolean" and f.get("default") is True}
    assert on - known_elsewhere <= set(gc.ABSENT_DEFAULTS)


class TestBackfillPlan:
    def _plan(self, stored, visited, gids=(1, 7, 255)):
        from scripts.backfill_config_absent_defaults import plan_rows
        return {(g, k): (v, why) for g, k, v, why in plan_rows(list(gids), stored, visited)}

    def test_site_editors_get_what_the_site_showed(self):
        plan = self._plan({}, {255})
        assert plan[(255, "notify_pbs")] == ("true", "site")
        assert plan[(255, "min_ca_tier_to_notify")] == ("EASY", "site")
        assert plan[(255, "seasonal_notify_pets")] == ("true", "site")

    def test_template_group_gets_defaults(self):
        assert self._plan({}, set())[(1, "notify_clogs")] == ("true", "site")

    def test_others_keep_running_off(self):
        plan = self._plan({}, set())
        assert plan[(7, "notify_pbs")] == ("false", "kept")
        assert plan[(7, "min_ca_tier_to_notify")] == ("EASY", "kept")

    def test_explicit_rows_untouched_and_cas_on_gets_tier(self):
        plan = self._plan({(7, "notify_pbs"): "0", (7, "notify_cas"): "true"}, set())
        assert (7, "notify_pbs") not in plan
        assert (7, "notify_cas") not in plan
        assert plan[(7, "min_ca_tier_to_notify")] == ("EASY", "cas-on")
