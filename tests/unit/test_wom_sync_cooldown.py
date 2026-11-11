"""The WOM sync rate limit, and what a rate-limited sync reports back.

Group membership refreshes are capped at one per group per hour
(``db.ops.sync_group_from_wom_with_stats``). Two things went wrong with that in
production and both are locked in here:

1. The dashboard rendered a refused request as ``+0 / -0 (0 total)``, which is
   also exactly what a sync that legitimately found nothing to change reports.
   A leader who had just enabled auto-provisioning read that as "the setting is
   broken" and kept re-clicking, every click refused. The route now says whether
   the sync actually ran.
2. Enabling ``auto_provision_members`` changes what a sync *does* — it starts
   creating profiles for roster members who have never installed the plugin — so
   the previous sync's cooldown must not gate the first run under the new
   setting.

The route tests use the scripted-session harness shared by the other web_api
route tests (``tests/unit/test_event_auth_modes``).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.group_admin as ga
from web_api.routes.config import _auto_provision_turned_on

from tests.unit.test_event_auth_modes import _S, _SessionCM


# ── The rate-limit hook on the config PATCH ──────────────────────────────────

class TestAutoProvisionTurnedOn:
    def test_off_to_on_clears_the_cooldown(self):
        # The production case: the row existed and was off.
        assert _auto_provision_turned_on({"auto_provision_members": "1"}, "0") is True

    def test_first_ever_save_counts_as_off_to_on(self):
        # No row yet — `before` is None, not "0".
        assert _auto_provision_turned_on({"auto_provision_members": "1"}, None) is True

    def test_legacy_true_spelling_is_read_as_already_on(self):
        # Rows written before boolean coercion stored "true"; re-saving one must
        # not read as a fresh enable and hand out a free sync.
        assert _auto_provision_turned_on({"auto_provision_members": "1"}, "true") is False

    def test_re_saving_an_enabled_setting_is_not_a_rate_limit_bypass(self):
        assert _auto_provision_turned_on({"auto_provision_members": "1"}, "1") is False

    def test_turning_it_off_does_not_clear_the_cooldown(self):
        assert _auto_provision_turned_on({"auto_provision_members": "0"}, "1") is False

    def test_unrelated_keys_leave_the_cooldown_alone(self):
        assert _auto_provision_turned_on({"discord_url": "https://x"}, None) is False


# ── The dashboard's Sync-from-WOM button ─────────────────────────────────────

def _wire(monkeypatch, *, wom_id=9028, result=None, user_id=7):
    """Authorize the request and script db.ops with a canned sync result.

    The route imports ``sync_group_from_wom_with_stats`` lazily from ``db.ops``,
    so the stub has to be installed on that module in sys.modules.
    """
    monkeypatch.setattr(ga, "current_user_id", lambda: user_id)
    monkeypatch.setattr(
        ga, "db_session",
        lambda: _SessionCM(_S([SimpleNamespace(group_id=42, wom_id=wom_id)])),
    )
    monkeypatch.setattr(ga, "manageable_guild_ids", lambda uid: [])
    monkeypatch.setattr(
        ga, "load_user",
        lambda s, uid: SimpleNamespace(id=uid, username="owner", is_superadmin=True),
    )
    monkeypatch.setattr(ga, "assert_group_admin", lambda *a, **k: None)
    monkeypatch.setattr(ga, "Group", SimpleNamespace(group_id=MagicMock()))

    ops = sys.modules.setdefault("db.ops", MagicMock())

    async def _sync(wom_id):
        return result

    monkeypatch.setattr(ops, "sync_group_from_wom_with_stats", _sync, raising=False)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


class TestWomSyncResponse:
    async def test_a_cooldown_says_so_instead_of_reporting_zero_changes(
        self, client, monkeypatch
    ):
        # The bug: this response was indistinguishable from a sync that ran and
        # found nothing, and reported the group as having 0 members because
        # total_members was absent from the cooldown result.
        _wire(monkeypatch, result={
            "on_cooldown": True,
            "cooldown_remaining_seconds": 2031,
            "group_name": "The Lobster Pot",
            "group_id": 42,
            "wom_id": 9028,
            "added": [],
            "removed": [],
            "total_members": 93,
            "skipped_removals": False,
        })
        r = await client.post("/api/v1/groups/42/wom-sync")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["on_cooldown"] is True
        assert body["cooldown_remaining_seconds"] == 2031
        # The group did not lose its members just because the sync was refused.
        assert body["total"] == 93
        assert body["added"] == 0 and body["removed"] == 0

    async def test_added_and_removed_are_counts_not_the_name_lists(
        self, client, monkeypatch
    ):
        # sync_group_from_wom_with_stats returns lists of player names. The route
        # used to call int() on them, so the first sync that actually added
        # anyone raised TypeError and the button 500ed — the failure mode was
        # invisible because most syncs change nothing.
        _wire(monkeypatch, result={
            "on_cooldown": False,
            "group_name": "The Lobster Pot",
            "group_id": 42,
            "wom_id": 9028,
            "added": ["Zezima", "Woox", "B0aty"],
            "removed": ["Framed"],
            "total_members": 427,
            "skipped_removals": False,
            "duration_seconds": 12.5,
        })
        r = await client.post("/api/v1/groups/42/wom-sync")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == 3
        assert body["removed"] == 1
        assert body["total"] == 427
        assert body["on_cooldown"] is False

    async def test_a_partial_wom_roster_is_surfaced(self, client, monkeypatch):
        # When WOM returns fewer members than it claims to have, the sync skips
        # the removal pass. The count alone would look like a clean no-op.
        _wire(monkeypatch, result={
            "on_cooldown": False,
            "group_name": "The Lobster Pot",
            "group_id": 42,
            "wom_id": 9028,
            "added": [],
            "removed": [],
            "total_members": 427,
            "skipped_removals": True,
            "duration_seconds": 3.0,
        })
        body = await (await client.post("/api/v1/groups/42/wom-sync")).get_json()
        assert body["skipped_removals"] is True


# ── One manual refresh a day (2026-11-11) ────────────────────────────────────
#
# The scheduled sync moved to every 12 hours (6 on a paid tier) when WiseOldMan
# asked us to cut volume, and the manual refresh became the escape hatch: one
# per group per 24 hours. ``db.ops`` is a MagicMock under conftest, so these
# pin the source, like the parity guards in test_wom_sync_detached_group.

import ast
from pathlib import Path

_OPS = (Path(__file__).resolve().parents[2] / "db" / "ops.py").read_text()


def _fn(name):
    for node in ast.walk(ast.parse(_OPS)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class TestDailyManualRefresh:
    def test_the_default_cooldown_is_24_hours(self):
        assert '_WOM_SYNC_COOLDOWN_SECONDS = int(os.getenv("WOM_MANUAL_SYNC_COOLDOWN_SECONDS", "86400"))' in _OPS

    def test_a_wom_outage_does_not_spend_the_daily_refresh(self):
        # The "no roster came back" raise must come BEFORE the cooldown stamp.
        src = ast.get_source_segment(_OPS, _fn("sync_group_from_wom_with_stats"))
        assert src.index("raise WomRosterUnavailable") < src.index('config_key="last_wom_sync",\n            config_value=now_iso')

    def test_the_sync_reports_whether_wom_answered(self):
        returns = [ast.get_source_segment(_OPS, n) for n in ast.walk(_fn("_sync_group_from_wom"))
                   if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
        assert len(returns) == 2 and all('"fetched"' in r for r in returns)

    def test_every_sync_stamps_the_schedule_so_a_manual_one_resets_the_clock(self):
        src = ast.get_source_segment(_OPS, _fn("_sync_group_from_wom"))
        assert "_stamp_member_sync(wom_id, ok=True)" in src and "_stamp_member_sync(wom_id, ok=False)" in src

    def test_only_the_scheduled_round_is_filtered_never_a_forced_sync(self):
        src = ast.get_source_segment(_OPS, _fn("update_group_members"))
        forced, scheduled = src.index("group_ids = [forced_id]"), src.index("_scheduled_sync_due(")
        assert forced < scheduled and src.count("_scheduled_sync_due(") == 1
