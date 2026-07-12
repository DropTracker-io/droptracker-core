"""Unit tests for the Discord scheduled-event mirror helpers.

Loaded directly from the file path (like test_event_lifecycle_sweep.py) so the
conftest sys.modules stubs for db/services never interfere — the module's
top-level imports are stdlib-only by design.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_scheduled_events.py",
)
_spec = importlib.util.spec_from_file_location("_event_sched_under_test", _MODULE_PATH)
se = importlib.util.module_from_spec(_spec)
sys.modules["_event_sched_under_test"] = se
_spec.loader.exec_module(se)


def ev(**overrides):
    base = dict(
        id=7, status="draft", discord_guild_id=None, starts_at=None, ends_at=None,
        discord_event_policy="on_activate",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDesiredGuildIds:
    def test_no_guild_desires_nothing(self):
        assert se.desired_guild_ids(ev()) == set()

    def test_guild_is_desired_as_string(self):
        # Snowflakes may arrive as ints from older rows; always compared as str.
        assert se.desired_guild_ids(ev(status="active", discord_guild_id=123)) == {"123"}
        assert se.desired_guild_ids(ev(status="active", discord_guild_id="123")) == {"123"}

    def test_active_event_still_desired(self):
        assert se.desired_guild_ids(ev(status="active", discord_guild_id="1")) == {"1"}

    def test_past_event_desires_nothing(self):
        # A post-end edit must not resurrect retired rows.
        assert se.desired_guild_ids(ev(status="past", discord_guild_id="1")) == set()

    def test_draft_desires_nothing_by_default(self):
        # discord_event_policy 'on_activate' (default): drafts never surface
        # on Discord — activation re-syncs and seeds the rows then. This is
        # also what stops abandoned drafts resurrecting via later syncs.
        assert se.desired_guild_ids(ev(discord_guild_id="1")) == set()
        # The gate suppresses opted-in participant guilds too.
        assert se.desired_guild_ids(ev(discord_guild_id="1"), participant_guild_ids=["2"]) == set()

    def test_draft_with_immediate_policy_is_desired(self):
        assert se.desired_guild_ids(
            ev(discord_guild_id="1", discord_event_policy="immediate")
        ) == {"1"}

    def test_missing_policy_attr_defaults_to_on_activate(self):
        # Rows predating the column (or stub events) gate like the default.
        e = SimpleNamespace(id=7, status="draft", discord_guild_id="1")
        assert se.desired_guild_ids(e) == set()

    def test_participants_join_the_desired_set(self):
        assert se.desired_guild_ids(
            ev(status="active", discord_guild_id="1"), participant_guild_ids=["2", None]
        ) == {"1", "2"}


class TestGuildSyncPlan:
    def test_new_guild_creates_row(self):
        plan = se.guild_sync_plan({"1"}, {})
        assert plan == {"create": ["1"], "pend": [], "retire": []}

    def test_edit_re_pends_synced_row(self):
        plan = se.guild_sync_plan({"1"}, {"1": "synced"})
        assert plan == {"create": [], "pend": ["1"], "retire": []}

    def test_edit_retries_failed_row(self):
        plan = se.guild_sync_plan({"1"}, {"1": "failed"})
        assert plan["pend"] == ["1"]

    def test_removed_guild_is_retired(self):
        plan = se.guild_sync_plan(set(), {"1": "synced"})
        assert plan == {"create": [], "pend": [], "retire": ["1"]}

    def test_guild_repoint_swaps_rows(self):
        plan = se.guild_sync_plan({"2"}, {"1": "synced"})
        assert plan == {"create": ["2"], "pend": [], "retire": ["1"]}

    def test_delete_pending_rows_are_left_alone(self):
        # Not re-pended when desired again (the bot is about to drop the row),
        # not re-retired when undesired.
        plan = se.guild_sync_plan({"1"}, {"1": "delete_pending", "2": "delete_pending"})
        assert plan == {"create": [], "pend": [], "retire": []}


class TestSchedulable:
    def test_no_start_is_not_schedulable(self):
        assert not se.schedulable(ev())

    def test_past_start_is_not_schedulable(self):
        assert not se.schedulable(ev(starts_at=datetime.now() - timedelta(hours=1)))

    def test_future_start_is_schedulable(self):
        assert se.schedulable(ev(starts_at=datetime.now() + timedelta(hours=1)))


class TestFutureEnd:
    """Partial-edit end for already-started scheduled events: start_time can
    no longer change on Discord, but a moved end (and name/description edits,
    which this helper supports from the reconciler) must keep syncing."""

    def test_no_end_is_none(self):
        assert se.future_end(ev()) is None

    def test_past_end_is_none(self):
        assert se.future_end(ev(ends_at=datetime.now() - timedelta(hours=1))) is None

    def test_future_end_is_aware_utc(self):
        end_local = datetime.now().replace(microsecond=0) + timedelta(hours=3)
        out = se.future_end(ev(ends_at=end_local))
        assert out is not None and out.tzinfo == timezone.utc
        assert out.astimezone().replace(tzinfo=None) == end_local


class TestSchedFields:
    START = datetime.now().replace(microsecond=0) + timedelta(days=1)

    def test_times_are_aware_utc(self):
        # DB datetimes are naive local; Discord parses an offset-less
        # isoformat as UTC, so both must come back aware-UTC and represent
        # the same instant as the local input.
        start, end, _ = se.sched_fields(
            ev(starts_at=self.START, ends_at=self.START + timedelta(hours=3))
        )
        assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc
        assert start.astimezone().replace(tzinfo=None) == self.START
        assert end - start == timedelta(hours=3)

    def test_missing_end_falls_back_to_default_duration(self):
        start, end, _ = se.sched_fields(ev(starts_at=self.START))
        assert end - start == se.DEFAULT_EVENT_DURATION

    def test_end_not_after_start_is_corrected(self):
        start, end, _ = se.sched_fields(
            ev(starts_at=self.START, ends_at=self.START - timedelta(hours=1))
        )
        assert end - start == se.DEFAULT_EVENT_DURATION

    def test_location_is_the_event_page(self):
        _, _, location = se.sched_fields(ev(id=42, starts_at=self.START))
        assert location == "https://www.droptracker.io/events/42"


# ── event_created_ping (companion message spec) ─────────────────────────────

class TestEventCreatedPing:
    PINGS = '{"event_created": ["901", "902"]}'
    CHANNELS = {"announcements": "555", "admin": "666"}

    def _ev(self, **kw):
        base = dict(status="active", discord_guild_id="111", name="Clash",
                    ping_config=self.PINGS)
        base.update(kw)
        return ev(**base)

    def test_pings_in_primary_guild_announcements(self):
        channel_id, content = se.event_created_ping(self._ev(), "111", "777", self.CHANNELS)
        assert channel_id == "555"
        assert "<@&901> <@&902>" in content
        assert "https://discord.com/events/111/777" in content
        assert "Clash" in content

    def test_mirror_guild_never_pings(self):
        # Opt-in clan-vs-clan mirrors have no channel config in their guild.
        assert se.event_created_ping(self._ev(), "222", "777", self.CHANNELS) == (None, None)

    def test_no_roles_configured_is_silent(self):
        assert se.event_created_ping(
            self._ev(ping_config=None), "111", "777", self.CHANNELS
        ) == (None, None)
        assert se.event_created_ping(
            self._ev(ping_config="corrupt"), "111", "777", self.CHANNELS
        ) == (None, None)

    def test_no_announcements_channel_is_silent(self):
        assert se.event_created_ping(self._ev(), "111", "777", {"admin": "666"}) == (None, None)
        assert se.event_created_ping(self._ev(), "111", "777", {}) == (None, None)

    def test_missing_ids_are_silent(self):
        assert se.event_created_ping(self._ev(), None, "777", self.CHANNELS) == (None, None)
        assert se.event_created_ping(self._ev(), "111", None, self.CHANNELS) == (None, None)


# ── sync_event_guilds against a stub session ─────────────────────────────────
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.added = []

    def query(self, *a, **k):
        return _FakeQuery(self.rows)

    def add(self, obj):
        self.added.append(obj)


def row(guild_id, sync_status, **extra):
    return SimpleNamespace(guild_id=guild_id, sync_status=sync_status, **extra)


class _FakeEventGuild(SimpleNamespace):
    """Column attributes exist on the class so filter expressions like
    ``EventGuild.event_id == event.id`` evaluate (the fake query ignores
    them); constructing one records the insert kwargs."""

    event_id = guild_id = sync_status = None


class TestSyncEventGuilds:
    def _patch_model(self, monkeypatch):
        # The conftest stubs db.models with a MagicMock; swap EventGuild for a
        # fake that records its kwargs so inserts can be asserted.
        monkeypatch.setattr(
            sys.modules["db.models"], "EventGuild", _FakeEventGuild, raising=False,
        )

    def test_create_pend_and_retire_in_one_pass(self, monkeypatch):
        self._patch_model(monkeypatch)
        old = row("111", "synced")
        s = _FakeSession([old])
        se.sync_event_guilds(s, ev(status="active", discord_guild_id="222"))
        assert old.sync_status == "delete_pending"
        assert [(r.guild_id, r.sync_status) for r in s.added] == [("222", "pending")]

    def test_edit_flips_synced_row_back_to_pending(self, monkeypatch):
        self._patch_model(monkeypatch)
        existing = row("111", "synced")
        s = _FakeSession([existing])
        se.sync_event_guilds(s, ev(status="active", discord_guild_id="111"))
        assert existing.sync_status == "pending"
        assert s.added == []

    def test_no_guild_creates_nothing(self, monkeypatch):
        self._patch_model(monkeypatch)
        s = _FakeSession()
        se.sync_event_guilds(s, ev())
        assert s.added == []

    def test_draft_sync_retires_existing_rows(self, monkeypatch):
        # A draft (default policy) that somehow has live rows — e.g. rows
        # created before the policy gate shipped — gets them retired on the
        # next sync instead of resurrected.
        self._patch_model(monkeypatch)
        zombie = row("111", "failed")
        s = _FakeSession([zombie])
        se.sync_event_guilds(s, ev(discord_guild_id="111"))
        assert zombie.sync_status == "delete_pending"
        assert s.added == []
