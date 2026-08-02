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


# ── envelope_from_plugin ──────────────────────────────────────────────────────

class TestEnvelopeFromPlugin:
    """Event-envelope used_api semantics: "came from the plugin". Both intake
    transports (direct API and the Discord webhook reader) count as plugin;
    only manual website/command submissions (intake_source == "manual") are
    non-plugin — regardless of the payload's transport-level used_api flag."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import envelope_from_plugin
        self.from_plugin = envelope_from_plugin

    def test_api_intake_is_plugin(self):
        assert self.from_plugin({"used_api": True}) is True

    def test_webhook_bot_intake_is_plugin(self):
        # The webhook bot stamps used_api=False (transport truth for the DB
        # row) — the envelope must still read as plugin traffic.
        assert self.from_plugin({"used_api": False}) is True

    def test_no_flags_is_plugin(self):
        assert self.from_plugin({}) is True

    def test_manual_intake_is_not_plugin(self):
        # Manual web/command submissions set intake_source="manual"; the
        # intake route stamps used_api=True on the row but the envelope
        # must read as non-plugin.
        assert self.from_plugin({"used_api": True, "intake_source": "manual"}) is False


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

class _FakeColumn:
    """A model column whose comparisons render as readable strings.

    tests/conftest.py stubs `db` as a MagicMock, so the real ORM columns
    compare into opaque MagicMocks and any assertion on the query's shape
    passes vacuously. Patching the models with this makes the criteria
    inspectable.
    """

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return f"{self.name} == {other!r}"

    def __gt__(self, other):
        return f"{self.name} > {other!r}"


class _FakeModel:
    unique_id = _FakeColumn("unique_id")
    date_added = _FakeColumn("date_added")
    used_api = _FakeColumn("used_api")


# Model attribute on data.submissions.common backing each dedup type.
_MODEL_FOR_TYPE = {
    "drop": "Drop",
    "pb": "PersonalBestEntry",
    "clog": "CollectionLogEntry",
    "ca": "CombatAchievementEntry",
    "pet": "PlayerPet",
    "quest": "QuestCompletionEntry",
}


class _RecordingSession:
    """Session stub that records every filter criterion it is handed.

    Lets a test assert on the SHAPE of the dedup query (e.g. that it carries no
    date window) and count how many lookups actually reached the "database".
    """

    def __init__(self, existing=None):
        self.existing = existing
        self.criteria = []
        self.query_count = 0

    def query(self, model):
        self.query_count += 1
        return self

    def filter(self, *criteria):
        self.criteria.extend(criteria)
        return self

    def first(self):
        return self.existing


class TestEnsureCanCreate:
    @pytest.fixture(autouse=True)
    def _import(self):
        from data.submissions.common import ensure_can_create, unique_id_cache
        self.ensure_can_create = ensure_can_create
        # Module-global, but conftest's autouse reset_unique_id_cache already
        # clears it around every test — just bind it for assertions.
        self.cache = unique_id_cache

    async def test_new_unique_id_allows_creation(self, mock_session):
        result = await self.ensure_can_create(mock_session, "guid-new-001", "drop")
        assert result is True

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

    # ── replay safety (2026-08-02 outage) ────────────────────────────────────
    # The recovery from a 2h31m intake outage replayed failed submissions hours
    # later and wrote 9 drops twice. These cases pin the two reasons why.

    async def test_lookup_carries_no_date_window(self):
        """A row written 2+ hours ago must still block: no time bound at all.

        The dedup query used to filter `date_added > now() - 1 hour`, so a
        replay older than that could not see its own original and inserted a
        duplicate. Asserting on the query shape (rather than a wall-clock
        fixture) proves the window is gone without making the test time-
        dependent.
        """
        session = _RecordingSession(existing=object())
        with patch("data.submissions.common.Drop", _FakeModel):
            assert await self.ensure_can_create(session, "guid-old-row", "drop") is False
        rendered = [str(c) for c in session.criteria]
        assert any("unique_id" in r for r in rendered), (
            f"expected the dedup query to filter on unique_id, got: {rendered}"
        )
        assert not any("date_added" in r for r in rendered), (
            f"dedup lookup must not be time-bounded, got: {rendered}"
        )

    @pytest.mark.parametrize("sub_type", sorted(_MODEL_FOR_TYPE))
    async def test_old_row_still_blocks_every_type(self, sub_type):
        """Every type dedups unbounded, not just drops."""
        session = _RecordingSession(existing=object())
        with patch(f"data.submissions.common.{_MODEL_FOR_TYPE[sub_type]}", _FakeModel):
            assert await self.ensure_can_create(session, f"guid-old-{sub_type}", sub_type) is False
        rendered = [str(c) for c in session.criteria]
        assert any("unique_id" in r for r in rendered)
        assert not any("date_added" in r for r in rendered), (
            f"{sub_type} dedup lookup must not be time-bounded, got: {rendered}"
        )

    async def test_failed_submission_can_be_retried(self, mock_session):
        """A GUID that never produced a row must not read as already seen.

        The cache used to be written BEFORE the DB check and was only ever
        evicted by size, so a submission that failed after being cached — what
        an outage causes — had its legitimate retry rejected for the life of
        the process, turning a dropped submission into a permanently lost one.
        """
        guid = "guid-failed-then-retried"
        assert await self.ensure_can_create(mock_session, guid, "drop") is True
        # The submission now fails; no row is ever written (mock_session keeps
        # returning None). The retry must still be allowed through.
        assert await self.ensure_can_create(mock_session, guid, "drop") is True
        assert guid not in self.cache["drop"]

    async def test_confirmed_duplicate_is_cached(self):
        """The cache is read-through: only a CONFIRMED row is remembered."""
        session = _RecordingSession(existing=object())
        assert await self.ensure_can_create(session, "guid-known-dup", "drop") is False
        assert "guid-known-dup" in self.cache["drop"]
        # Second attempt short-circuits on the cache — no further DB lookup.
        assert await self.ensure_can_create(session, "guid-known-dup", "drop") is False
        assert session.query_count == 1

    @pytest.mark.parametrize("missing", [None, ""])
    async def test_missing_unique_id_is_never_a_duplicate(self, missing):
        """No GUID means no dedup key — allow it, and never query.

        `unique_id == None` renders as `IS NULL`, which matches the ~200k
        legacy rows carrying no GUID. Unbounded, that would reject every
        guid-less submission forever; the old one-hour window masked it.
        """
        session = _RecordingSession(existing=object())
        assert await self.ensure_can_create(session, missing, "pb") is True
        assert session.query_count == 0

    async def test_cache_is_size_bounded(self, mock_session):
        """Confirmed-duplicate caching must not grow without limit."""
        from data.submissions.common import _UNIQUE_ID_CACHE_SIZE
        session = _RecordingSession(existing=object())
        for i in range(_UNIQUE_ID_CACHE_SIZE + 50):
            await self.ensure_can_create(session, f"guid-bulk-{i}", "drop")
        assert len(self.cache["drop"]) == _UNIQUE_ID_CACHE_SIZE


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
