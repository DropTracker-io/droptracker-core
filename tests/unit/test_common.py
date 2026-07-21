"""
Unit tests for data/submissions/common.py — auth, dedup, and helper functions.

All DB / Redis / external API calls are replaced by the sys.modules stubs
configured in tests/conftest.py.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ── is_truthy_config ──────────────────────────────────────────────────────────

class TestIsTruthyConfig:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import is_truthy_config
        self.is_truthy_config = is_truthy_config

    def test_true_string(self):
        assert self.is_truthy_config("true") is True

    def test_true_uppercase(self):
        assert self.is_truthy_config("TRUE") is True

    def test_one_string(self):
        assert self.is_truthy_config("1") is True

    def test_false_string(self):
        assert self.is_truthy_config("false") is False

    def test_zero_string(self):
        assert self.is_truthy_config("0") is False

    def test_none(self):
        assert self.is_truthy_config(None) is False

    def test_empty_string(self):
        assert self.is_truthy_config("") is False

    def test_whitespace_true(self):
        assert self.is_truthy_config("  true  ") is True

    def test_arbitrary_string(self):
        assert self.is_truthy_config("yes") is False


# ── get_config_prefix ─────────────────────────────────────────────────────────

class TestGetConfigPrefix:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import get_config_prefix
        self.get_config_prefix = get_config_prefix

    def test_main_world_empty_prefix(self):
        assert self.get_config_prefix("main") == ""

    def test_seasonal_world_prefix(self):
        assert self.get_config_prefix("seasonal") == "seasonal_"

    def test_unknown_world_empty_prefix(self):
        assert self.get_config_prefix("unknown") == ""

    def test_empty_string_world(self):
        assert self.get_config_prefix("") == ""


# ── _is_temp_account_hash ─────────────────────────────────────────────────────

class TestIsTempAccountHash:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import _is_temp_account_hash
        self.fn = _is_temp_account_hash

    def test_wom_temp_prefix_is_temp(self):
        assert self.fn("wom_temp_12345") is True

    def test_normal_hash_not_temp(self):
        assert self.fn("abc123def456") is False

    def test_none_not_temp(self):
        assert self.fn(None) is False

    def test_empty_not_temp(self):
        assert self.fn("") is False

    def test_partial_prefix_not_temp(self):
        assert self.fn("wom_12345") is False


# ── ensure_can_create ─────────────────────────────────────────────────────────

class TestEnsureCanCreate:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import ensure_can_create
        self.ensure_can_create = ensure_can_create

    async def test_new_unique_id_allows_creation(self, mock_session):
        result = await self.ensure_can_create(mock_session, "guid-new-001", "drop")
        assert result is True

    async def test_duplicate_in_cache_blocks_creation(self, mock_session):
        await self.ensure_can_create(mock_session, "guid-dup-001", "drop")
        result = await self.ensure_can_create(mock_session, "guid-dup-001", "drop")
        assert result is False

    async def test_different_types_do_not_collide(self, mock_session):
        await self.ensure_can_create(mock_session, "guid-shared-001", "drop")
        result = await self.ensure_can_create(mock_session, "guid-shared-001", "clog")
        assert result is True

    async def test_duplicate_in_db_blocks_creation(self, mock_session):
        # Simulate the DB finding an existing entry (non-None first())
        mock_session.query.return_value.filter.return_value.first.return_value = MagicMock()
        result = await self.ensure_can_create(mock_session, "guid-db-dup-001", "drop")
        assert result is False

    async def test_all_submission_types_accepted(self, mock_session):
        types = ["drop", "clog", "pb", "ca", "pet", "quest"]
        for sub_type in types:
            guid = f"guid-{sub_type}-unique"
            result = await self.ensure_can_create(mock_session, guid, sub_type)
            assert result is True, f"Expected True for type={sub_type}"

    async def test_seasonal_drop_type(self, mock_session):
        result = await self.ensure_can_create(mock_session, "guid-seasonal-001", "seasonal_drop")
        assert result is True


# ── check_auth ────────────────────────────────────────────────────────────────

class TestCheckAuth:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import check_auth
        self.check_auth = check_auth

    def test_matching_hash_returns_authed(self, mock_player, mock_session):
        user_exists, authed = self.check_auth(
            player_name="TestPlayer",
            account_hash="testhash123",
            auth_key=None,
            external_session=mock_session,
            resolved_player=mock_player,
        )
        assert user_exists is True
        assert authed is True

    def test_mismatched_hash_returns_not_authed(self, mock_player, mock_session):
        mock_player.account_hash = "stored-hash-xyz"
        user_exists, authed = self.check_auth(
            player_name="TestPlayer",
            account_hash="submitted-hash-abc",
            auth_key=None,
            external_session=mock_session,
            resolved_player=mock_player,
        )
        assert user_exists is True
        assert authed is False

    def test_player_not_found_returns_false_false(self, mock_session):
        # Session returns None for player lookup; no resolved_player provided
        mock_session.query.return_value.filter.return_value.first.return_value = None
        user_exists, authed = self.check_auth(
            player_name="Ghost",
            account_hash="hash123",
            auth_key=None,
            external_session=mock_session,
        )
        assert user_exists is False
        assert authed is False

    def test_no_stored_hash_binds_on_first_submission(self, mock_player, mock_session):
        # Player has no stored hash yet (first-ever submission)
        mock_player.account_hash = None
        # No conflicting player with this hash in DB
        mock_session.query.return_value.filter.return_value.first.return_value = None

        user_exists, authed = self.check_auth(
            player_name="TestPlayer",
            account_hash="brand-new-hash",
            auth_key=None,
            external_session=mock_session,
            resolved_player=mock_player,
        )
        assert user_exists is True
        assert authed is True
        # Hash should have been bound
        assert mock_player.account_hash == "brand-new-hash"

    def test_temp_hash_replaced_on_real_submission(self, mock_player, mock_session):
        # Temp hashes (wom_temp_*) are replaced by the real hash on first plugin auth
        mock_player.account_hash = "wom_temp_99999"
        mock_session.query.return_value.filter.return_value.first.return_value = None

        user_exists, authed = self.check_auth(
            player_name="TestPlayer",
            account_hash="real-hash-from-plugin",
            auth_key=None,
            external_session=mock_session,
            resolved_player=mock_player,
        )
        assert user_exists is True
        assert authed is True


# ── get_group_drop_notify_settings ────────────────────────────────────────────

class TestGetGroupDropNotifySettings:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import get_group_drop_notify_settings
        self.fn = get_group_drop_notify_settings

    def test_defaults_when_no_config(self, mock_session):
        # No configuration rows → default values
        mock_session.query.return_value.filter.return_value.first.return_value = None
        min_val, send_stacks = self.fn(mock_session, group_id=5)
        assert min_val == 2500000
        assert send_stacks is False

    def test_custom_min_value(self, mock_session):
        config_mock = MagicMock()
        config_mock.config_value = "1000000"
        mock_session.query.return_value.filter.return_value.first.return_value = config_mock

        min_val, _ = self.fn(mock_session, group_id=5)
        assert min_val == 1000000

    def test_send_stacks_enabled(self, mock_session):
        # First query → min_value config (None → use default)
        # Second query → send_stacks config ("true" → enabled)
        stacks_config = MagicMock()
        stacks_config.config_value = "true"
        mock_session.query.return_value.filter.return_value.first.side_effect = [
            None,
            stacks_config,
        ]

        _, send_stacks = self.fn(mock_session, group_id=5)
        assert send_stacks is True

    def test_send_stacks_disabled_by_zero(self, mock_session):
        config_mock = MagicMock()
        config_mock.config_value = "0"
        mock_session.query.return_value.filter.return_value.first.return_value = config_mock

        _, send_stacks = self.fn(mock_session, group_id=5)
        assert send_stacks is False


# ── create_notification ───────────────────────────────────────────────────────

def _configure_channels(mock_session, rows):
    """Make the group-channel lookup in create_notification return `rows`.

    Rows are (config_key, config_value) tuples, matching the
    GroupConfiguration query in group_has_notification_channel.
    """
    mock_session.query.return_value.filter.return_value.all.return_value = rows


class TestCreateNotification:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import create_notification
        self.fn = create_notification

    async def test_creates_notification_in_session(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_loot", "123456789012345678")])
        data = {"item_name": "Dragon claws", "player_name": "Zezima", "value": 50_000_000}
        notification_id = await self.fn(
            "drop",
            player_id=42,
            data=data,
            group_id=5,
            existing_session=mock_session,
        )
        # Should have called session.add() with a NotificationQueue object
        assert mock_session.add.called

    async def test_duplicate_data_hash_not_double_created(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_loot", "123456789012345678")])
        data = {"item_name": "Twisted bow", "player_name": "Zezima", "value": 1_000_000_000}
        await self.fn("drop", player_id=42, data=data, group_id=5, existing_session=mock_session)
        call_count_after_first = mock_session.add.call_count
        assert call_count_after_first == 1

        # Same data again — should be deduplicated by hash
        await self.fn("drop", player_id=42, data=data, group_id=5, existing_session=mock_session)
        assert mock_session.add.call_count == call_count_after_first

    async def test_different_data_creates_separate_notifications(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_loot", "123456789012345678")])
        data_a = {"item_name": "Dragon claws", "player_name": "Alice", "value": 50_000_000}
        data_b = {"item_name": "Twisted bow", "player_name": "Alice", "value": 1_000_000_000}
        await self.fn("drop", player_id=1, data=data_a, group_id=5, existing_session=mock_session)
        await self.fn("drop", player_id=1, data=data_b, group_id=5, existing_session=mock_session)
        assert mock_session.add.call_count == 2


# ── enqueue skip: groups with no notification channel ─────────────────────────

class TestCreateNotificationChannelGate:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import create_notification
        self.fn = create_notification

    async def test_group_type_skipped_when_no_channel_configured(self, mock_session):
        _configure_channels(mock_session, [])
        result = await self.fn(
            "drop", player_id=42,
            data={"item_name": "Dragon claws", "player_name": "Zezima"},
            group_id=5, existing_session=mock_session,
        )
        assert result is None
        assert not mock_session.add.called

    async def test_group_type_skipped_when_channel_empty_or_zero(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_loot", "")])
        await self.fn("drop", player_id=42, data={"a": 1}, group_id=5, existing_session=mock_session)
        assert not mock_session.add.called

        _configure_channels(mock_session, [("channel_id_to_post_loot", "0")])
        await self.fn("drop", player_id=42, data={"a": 2}, group_id=5, existing_session=mock_session)
        assert not mock_session.add.called

    async def test_fallback_loot_channel_allows_typed_notification(self, mock_session):
        # quest has no dedicated channel but falls back to the loot channel
        _configure_channels(mock_session, [
            ("channel_id_to_post_quests", ""),
            ("channel_id_to_post_loot", "123456789012345678"),
        ])
        await self.fn(
            "quest", player_id=42,
            data={"quest_name": "Dragon Slayer II", "player_name": "Zezima"},
            group_id=5, existing_session=mock_session,
        )
        assert mock_session.add.called

    async def test_level_up_has_no_loot_fallback(self, mock_session):
        # level_up posts only to channel_id_to_post_levels (no loot fallback
        # in send_level_up_notification_with_session) — mirror that here.
        _configure_channels(mock_session, [("channel_id_to_post_loot", "123456789012345678")])
        await self.fn(
            "level_up", player_id=42,
            data={"player_name": "Zezima", "skills_text": "Attack 99"},
            group_id=5, existing_session=mock_session,
        )
        assert not mock_session.add.called

    async def test_dm_types_never_gated(self, mock_session):
        _configure_channels(mock_session, [])
        await self.fn(
            "dm_drop", player_id=42,
            data={"item_name": "Dragon claws", "player_name": "Zezima"},
            group_id=None, existing_session=mock_session,
        )
        assert mock_session.add.called

    async def test_system_types_never_gated(self, mock_session):
        _configure_channels(mock_session, [])
        await self.fn(
            "new_npc", player_id=42,
            data={"npc_name": "Zulrah", "player_name": "Zezima"},
            group_id=None, existing_session=mock_session,
        )
        assert mock_session.add.called

    async def test_lookup_error_does_not_block_enqueue(self, mock_session):
        mock_session.query.return_value.filter.return_value.all.side_effect = RuntimeError("db down")
        await self.fn(
            "drop", player_id=42,
            data={"item_name": "Dragon claws", "player_name": "Zezima"},
            group_id=5, existing_session=mock_session,
        )
        assert mock_session.add.called


class TestGroupHasNotificationChannel:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import group_has_notification_channel
        self.fn = group_has_notification_channel

    def test_unknown_type_is_never_gated(self, mock_session):
        _configure_channels(mock_session, [])
        assert self.fn(mock_session, 5, "monetary_contribution") is True

    def test_no_group_is_never_gated(self, mock_session):
        _configure_channels(mock_session, [])
        assert self.fn(mock_session, None, "drop") is True

    def test_configured_channel_passes(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_pets", "123456789012345678")])
        assert self.fn(mock_session, 5, "pet") is True

    def test_missing_rows_fail(self, mock_session):
        _configure_channels(mock_session, [])
        assert self.fn(mock_session, 5, "clog") is False

    def test_whitespace_value_fails(self, mock_session):
        _configure_channels(mock_session, [("channel_id_to_post_loot", "   ")])
        assert self.fn(mock_session, 5, "drop") is False


# ── WOM EHB extraction / identity application ────────────────────────────────

class TestExtractEhb:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import _extract_ehb_from_wom_player
        self.fn = _extract_ehb_from_wom_player

    def test_identity_shim_dict(self):
        assert self.fn({"total_level": 2100, "ehb": 456.789}) == 456.79

    def test_pre_upgrade_cache_entry_lacks_key(self):
        # Cached identity dicts written before the upgrade have no "ehb" key —
        # None (unknown) so a stored value is never overwritten with garbage.
        assert self.fn({"total_level": 2100}) is None

    def test_garbage_value_is_none(self):
        assert self.fn({"ehb": "not-a-number"}) is None

    def test_raw_wom_object(self):
        from types import SimpleNamespace
        assert self.fn(SimpleNamespace(ehb=12.5)) == 12.5

    def test_none_input(self):
        assert self.fn(None) is None

    def test_zero_is_a_real_value(self):
        # A bossless account genuinely has 0.0 EHB — distinct from unknown.
        assert self.fn({"ehb": 0}) == 0.0


class TestApplyIdentityEhb:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import _apply_authoritative_wom_identity
        self.fn = _apply_authoritative_wom_identity

    def _player(self, **kw):
        from types import SimpleNamespace
        base = dict(player_id=1, wom_id=5, player_name="Test",
                    total_level=2000, log_slots=100, ehb=None, account_hash=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_sets_ehb_when_unknown(self):
        p = self._player(ehb=None)
        _, changed = self.fn(MagicMock(), p, 5, ehb=321.5)
        assert changed is True
        assert p.ehb == 321.5

    def test_updates_ehb_when_different(self):
        p = self._player(ehb=100.0)
        _, changed = self.fn(MagicMock(), p, 5, ehb=105.25)
        assert changed is True
        assert p.ehb == 105.25

    def test_equal_ehb_is_not_a_change(self):
        p = self._player(ehb=100.0)
        _, changed = self.fn(MagicMock(), p, 5, ehb=100.0)
        assert changed is False

    def test_none_never_overwrites(self):
        p = self._player(ehb=100.0)
        _, changed = self.fn(MagicMock(), p, 5, ehb=None)
        assert changed is False
        assert p.ehb == 100.0
