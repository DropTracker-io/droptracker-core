"""Safe deaths are not announced unless a group asks for them.

Dying in Castle Wars, a raid wipe or your own house costs nothing, and a feed
full of those buries the deaths people care about. Dink filters them per-user
with ``deathIgnoreSafe``; this is the group-wide equivalent. Three things have
to hold, and all three are asserted here:

1. "is this death safe?" prefers the client's verdict and falls back to the
   region — but never guesses "safe" when it has neither, because that would
   silently swallow a real death;
2. the default is to withhold safe deaths, and ``notify_deaths_safe`` turns
   them back on;
3. nothing here can reach a member's own submission DM.
"""

from unittest.mock import MagicMock, patch

import pytest

from db.death_filter import (
    SAFE_DEATH_CONFIG_KEY,
    death_skip_reason,
    is_safe_death,
    parse_flag,
)

# Region ids used below, so a renumbering shows up here rather than silently
# reclassifying every death.
CASTLE_WARS = 9520
CHAMBERS_OF_XERIC = 12889
INFERNO = 9043
FIGHT_CAVES = 9551
VORKATH = 9023


@pytest.fixture
def group_config(monkeypatch):
    """Stub ``group_config.get`` with a ``{(group_id, key): value}`` map."""
    from utils import group_config as gc

    store: dict = {}

    def _get(_session, group_id, key, default=None):
        return store.get((group_id, key), default)

    monkeypatch.setattr(gc, "get", _get)
    return store


class TestParseFlag:
    @pytest.mark.parametrize("value", [True, "true", "True", " TRUE ", "1", 1, "yes"])
    def test_truthy_forms(self, value):
        assert parse_flag(value) is True

    @pytest.mark.parametrize("value", [False, "false", "False", "0", 0, "no"])
    def test_falsy_forms(self, value):
        assert parse_flag(value) is False

    @pytest.mark.parametrize("value", [None, "", "N/A", "maybe", "  "])
    def test_absent_is_neither(self, value):
        # The distinction that matters: a field the plugin had no value for
        # ("N/A") must not read as False, or an old client's death would be
        # treated as a confirmed dangerous one.
        assert parse_flag(value) is None


class TestIsSafeDeath:
    def test_client_verdict_wins(self):
        # The client can see account type and Pest Control; the server cannot.
        # A hardcore ironman dying in a raid is a real death, and only the
        # plugin knows that — so its "false" overrides the safe region.
        assert is_safe_death({"is_safe_death": "false", "region_id": CHAMBERS_OF_XERIC}) is False
        assert is_safe_death({"is_safe_death": "true", "region_id": VORKATH}) is True

    def test_falls_back_to_the_region_when_the_flag_is_missing(self):
        # A pre-6.0 client sends region_id and no verdict.
        assert is_safe_death({"region_id": CASTLE_WARS}) is True
        assert is_safe_death({"region_id": VORKATH}) is False

    def test_na_is_treated_as_missing_not_as_false(self):
        assert is_safe_death({"is_safe_death": "N/A", "region_id": CASTLE_WARS}) is True

    def test_a_death_with_no_signal_at_all_is_dangerous(self):
        # Nothing known about it, so it gets announced. Guessing "safe" here
        # would drop real deaths on the floor.
        assert is_safe_death({}) is False
        assert is_safe_death({"region_id": "N/A"}) is False

    @pytest.mark.parametrize("region_id", [INFERNO, FIGHT_CAVES])
    def test_inferno_and_fight_caves_are_never_treated_as_safe(self, region_id):
        # Mechanically safe, but losing the run is the whole point of the post.
        # This is Dink's default deathSafeExceptions, baked in; a group that
        # disagrees blacklists the region instead.
        assert is_safe_death({"region_id": region_id}) is False


class TestDeathSkipReason:
    SAFE = {"is_safe_death": "true", "region_id": CASTLE_WARS, "region_name": "Castle Wars"}
    DANGEROUS = {"is_safe_death": "false", "region_id": VORKATH, "region_name": "Ungael"}

    def test_safe_deaths_are_withheld_by_default(self, group_config):
        reason = death_skip_reason(None, 303, "death", self.SAFE)
        assert reason == "safe death (Castle Wars)"

    def test_a_group_can_turn_them_back_on(self, group_config):
        group_config[(303, SAFE_DEATH_CONFIG_KEY)] = "true"
        assert death_skip_reason(None, 303, "death", self.SAFE) is None

    def test_dangerous_deaths_are_never_withheld(self, group_config):
        assert death_skip_reason(None, 303, "death", self.DANGEROUS) is None

    def test_seasonal_deaths_read_the_seasonal_key(self, group_config):
        # A group can want safe deaths on Leagues and not on the main game.
        group_config[(303, f"seasonal_{SAFE_DEATH_CONFIG_KEY}")] = "true"
        seasonal = {**self.SAFE, "world_type": "seasonal"}
        assert death_skip_reason(None, 303, "death", seasonal) is None
        # …and the main-game key is untouched by that.
        assert death_skip_reason(None, 303, "death", self.SAFE) is not None

    def test_personal_dms_are_out_of_scope(self, group_config):
        # A group's settings must never reach a member's own submission DM.
        assert death_skip_reason(None, 303, "dm_death", self.SAFE) is None

    def test_other_notification_types_are_untouched(self, group_config):
        assert death_skip_reason(None, 303, "drop", self.SAFE) is None
        assert death_skip_reason(None, 303, "clog", self.SAFE) is None

    def test_no_group_means_no_filtering(self, group_config):
        assert death_skip_reason(None, None, "death", self.SAFE) is None

    def test_reason_omits_the_place_when_unknown(self, group_config):
        assert death_skip_reason(None, 303, "death", {"is_safe_death": True}) == "safe death"

    def test_a_config_fault_applies_the_default(self, monkeypatch, group_config):
        # Not fail-open: the alternative is that a database blip floods every
        # group's feed with exactly the deaths they configured away.
        from utils import group_config as gc

        def _boom(*_a, **_k):
            raise RuntimeError("database went away")

        monkeypatch.setattr(gc, "get", _boom)
        assert death_skip_reason(None, 303, "death", self.SAFE) is not None
        # A dangerous death is still sent — the config is never even consulted.
        assert death_skip_reason(None, 303, "death", self.DANGEROUS) is None

    def test_old_client_safe_death_is_still_withheld(self, group_config):
        # No is_safe_death field at all: the region fallback is what stops a
        # pre-6.0 member's Castle Wars deaths escaping the group's setting.
        assert death_skip_reason(None, 303, "death", {"region_id": CASTLE_WARS}) is not None


# ── The chokepoint ──────────────────────────────────────────────────────────

class TestCreateNotificationHonoursSafeDeaths:
    """The gate as the death processor actually hits it."""

    @pytest.fixture
    def common(self):
        import data.submissions.common as common

        return common

    async def _create(self, common, reason, *, notification_type="death"):
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "player_hidden_for_group", return_value=False), \
             patch.object(common, "notification_blacklisted", return_value=None), \
             patch.object(common, "safe_death_filtered", return_value=reason), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            result = await common.create_notification(
                notification_type,
                77,
                {"is_safe_death": True, "region_id": CASTLE_WARS},
                303,
                existing_session=db,
            )
        return result, db

    async def test_a_withheld_safe_death_enqueues_nothing(self, common):
        result, db = await self._create(common, "safe death (Castle Wars)")
        assert result is None
        db.add.assert_not_called()

    async def test_a_dangerous_death_still_enqueues(self, common):
        _, db = await self._create(common, None)
        assert db.add.called

    async def test_the_real_rule_is_wired_in_not_just_the_patch(self, common, group_config):
        # Guards the wiring itself: patch only the blacklist and let the real
        # safe-death filter run, so removing the call from create_notification
        # fails here rather than passing on a mocked return value.
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "player_hidden_for_group", return_value=False), \
             patch.object(common, "notification_blacklisted", return_value=None), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            result = await common.create_notification(
                "death", 77, {"is_safe_death": "true", "region_id": CASTLE_WARS},
                303, existing_session=db,
            )
        assert result is None
        db.add.assert_not_called()
