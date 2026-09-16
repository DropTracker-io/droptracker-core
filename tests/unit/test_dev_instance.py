"""scripts/dev_instance.py — the pieces that decide something.

The script talks to Discord and the dev database, which the unit suite does
not have; what is pinned here is the arithmetic and the permission shapes it
would apply.
"""
from __future__ import annotations

import pytest

from tests.unit import _tester_db as tdb

di = tdb.load("_dev_instance_under_test", "scripts", "dev_instance.py")


class TestOffsetTarget:
    @pytest.mark.parametrize("current,target", [
        (1, 10_000_000),
        (3_422, 10_000_000),
        (99_002, 10_000_000),
        (5_000_000, 10_000_000),
        (5_763_126, 20_000_000),
        (12_000_000, 30_000_000),
    ])
    def test_targets(self, current, target):
        assert di.offset_target(current) == target

    def test_always_leaves_headroom(self):
        for current in (1, 4_999_999, 5_000_001, 7_654_321, 49_999_999):
            assert di.offset_target(current) >= 2 * current

    def test_the_reserved_groups_sit_above_the_group_offset(self):
        assert di.BUG_TESTERS_GROUP_ID > di.offset_target(99_002)
        assert di.FIREHOSE_GROUP_ID > di.offset_target(99_002)


class TestChannelPermissions:
    EVERYONE, TESTER, STAFF, BOT = "1", "2", "3", "4"

    def _overwrites(self, access):
        rows = di._channel_overwrites(access, self.EVERYONE, self.TESTER, [self.STAFF], [self.BOT])
        return {row["id"]: (int(row["allow"]), int(row["deny"]), row["type"]) for row in rows}

    def test_start_here_is_readable_by_everyone_but_not_writable(self):
        ow = self._overwrites("open_read")
        allow, deny, _ = ow[self.EVERYONE]
        assert allow & di.VIEW and deny & di.SEND

    @pytest.mark.parametrize("access", ["testers", "testers_read", "staff"])
    def test_everyone_else_cannot_see_tester_channels(self, access):
        allow, deny, _ = self._overwrites(access)[self.EVERYONE]
        assert deny & di.VIEW and not allow & di.VIEW

    def test_testers_can_talk_in_their_channels(self):
        allow, deny, _ = self._overwrites("testers")[self.TESTER]
        assert allow & di.SEND and allow & di.VIEW and not deny

    def test_announcement_channels_are_read_only_for_testers(self):
        allow, deny, _ = self._overwrites("testers_read")[self.TESTER]
        assert allow & di.VIEW and deny & di.SEND

    def test_the_firehose_is_hidden_from_testers(self):
        allow, deny, _ = self._overwrites("staff")[self.TESTER]
        assert deny & di.VIEW and not allow & di.VIEW

    @pytest.mark.parametrize("access", ["open_read", "testers", "testers_read", "staff"])
    def test_the_bots_can_always_post(self, access):
        allow, _, kind = self._overwrites(access)[self.BOT]
        assert kind == 1, "a member overwrite, not a role"
        assert allow & di.SEND and allow & di.VIEW and allow & di.EMBED

    @pytest.mark.parametrize("access", ["open_read", "testers", "testers_read", "staff"])
    def test_staff_can_always_post(self, access):
        allow, _, kind = self._overwrites(access)[self.STAFF]
        assert kind == 0 and allow & di.SEND


class TestGroupSpecs:
    def test_every_channel_a_group_names_exists_in_the_layout(self):
        keys = {key for key, *_ in di.CHANNELS}
        for spec in di.GROUP_SPECS:
            for channel_key in spec["channels"].values():
                assert channel_key is None or channel_key in keys

    def test_the_firehose_never_posts_where_testers_read(self):
        firehose = next(s for s in di.GROUP_SPECS if s["group_id"] == di.FIREHOSE_GROUP_ID)
        staff_only = {key for key, _, _, access, _ in di.CHANNELS if access == "staff"}
        for channel_key in firehose["channels"].values():
            assert channel_key is None or channel_key in staff_only

    def test_the_firehose_keeps_its_threshold(self):
        firehose = next(s for s in di.GROUP_SPECS if s["group_id"] == di.FIREHOSE_GROUP_ID)
        assert int(firehose["values"]["minimum_value_to_notify"]) >= 5_000_000

    def test_the_start_here_message_names_only_real_channels(self):
        keys = {key for key, *_ in di.CHANNELS}
        rendered = di.START_HERE_MESSAGE.format(**{k: f"<#{k}>" for k in keys})
        assert "{" not in rendered

    def test_group_names_fit_the_column(self):
        for spec in di.GROUP_SPECS:
            assert len(spec["name"]) <= 30


class TestBotPermissions:
    GUILD = "100"

    def test_everyone_plus_own_roles(self):
        roles = [{"id": self.GUILD, "permissions": str(di.VIEW)},
                 {"id": "7", "permissions": str(di.SEND)},
                 {"id": "8", "permissions": str(di.MANAGE_THREADS)}]
        assert di.guild_permissions(roles, ["7"], self.GUILD) == di.VIEW | di.SEND

    def test_administrator_holds_everything(self):
        roles = [{"id": self.GUILD, "permissions": "0"},
                 {"id": "7", "permissions": str(di.ADMINISTRATOR)}]
        held = di.guild_permissions(roles, ["7"], self.GUILD)
        assert held & di.MANAGE_THREADS and held & di.THREAD_SEND

    def test_overwrites_keep_only_what_the_bot_holds(self):
        held = di.VIEW | di.SEND
        rows = [di._overwrite("1", 0, allow=di.VIEW | di.MANAGE_THREADS, deny=di.SEND | di.ATTACH)]
        out, dropped = di.mask_overwrites(rows, held)
        assert int(out[0]["allow"]) == di.VIEW
        assert int(out[0]["deny"]) == di.SEND
        assert dropped == di.MANAGE_THREADS | di.ATTACH
