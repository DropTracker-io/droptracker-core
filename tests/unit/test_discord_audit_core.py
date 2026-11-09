"""services/discord_audit_core.py: what the audit log stores, who it says
deleted a message, and what its posts contain.

Loaded from the file path because the conftest stubs the ``services`` package.
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_discord_audit_core_under_test", "services", "discord_audit_core.py")

GUILD = "1172737525069135962"
AUDIT_CHANNEL = "111"
GENERAL = "222"
NOW = 1_762_700_000.0  # 2025-11-09


def _snowflake_at(unix_seconds: float, increment: int = 0) -> str:
    return str((int(unix_seconds * 1000) - core.DISCORD_EPOCH_MS) << 22 | increment)


def _settings(**over):
    row = SimpleNamespace(guild_id=GUILD, enabled=True, channel_id=AUDIT_CHANNEL, events=None,
                          ignored_channel_ids=None, retention_days=30)
    for k, v in over.items():
        setattr(row, k, v)
    return core.AuditSettings.from_row(row)


# --------------------------------------------------------------------------- settings

class TestEvents:
    def test_defaults_are_all_on(self):
        assert core.resolve_events(None) == core.DEFAULT_EVENTS
        assert all(core.DEFAULT_EVENTS.values())

    def test_overrides_apply_and_unknown_keys_are_ignored(self):
        events = core.resolve_events({"member_join": False, "bogus": True})
        assert events["member_join"] is False
        assert "bogus" not in events

    def test_set_event_stores_only_changes(self):
        overrides = core.set_event({}, "member_join", False)
        assert overrides == {"member_join": False}
        assert core.set_event(overrides, "member_join", True) == {}

    def test_set_event_rejects_unknown_kind(self):
        try:
            core.set_event({}, "nope", True)
        except ValueError:
            return
        raise AssertionError("expected ValueError")

    def test_every_event_has_a_label(self):
        assert set(core.EVENT_LABELS) == set(core.DEFAULT_EVENTS)


class TestSettings:
    def test_active_needs_enabled_and_channel(self):
        assert _settings().active
        assert not _settings(enabled=False).active
        assert not _settings(channel_id=None).active

    def test_logs_respects_switches_and_pause(self):
        assert _settings().logs("message_delete")
        assert not _settings(events={"message_delete": False}).logs("message_delete")
        assert not _settings(enabled=False).logs("message_delete")

    def test_retention_never_below_one_day(self):
        assert _settings(retention_days=0).retention_days == 30  # unset -> default
        assert _settings(retention_days=-5).retention_days == 1


class TestShouldStore:
    def test_human_message_in_active_server_is_stored(self):
        assert core.should_store(_settings(), author_is_bot=False, is_webhook=False, channel_id=GENERAL)

    def test_bots_and_webhooks_are_not_stored(self):
        assert not core.should_store(_settings(), author_is_bot=True, is_webhook=False, channel_id=GENERAL)
        assert not core.should_store(_settings(), author_is_bot=False, is_webhook=True, channel_id=GENERAL)

    def test_nothing_is_stored_when_off_or_unconfigured(self):
        assert not core.should_store(None, author_is_bot=False, is_webhook=False, channel_id=GENERAL)
        assert not core.should_store(_settings(enabled=False), author_is_bot=False, is_webhook=False,
                                     channel_id=GENERAL)

    def test_audit_channel_never_logs_itself(self):
        assert not core.should_store(_settings(), author_is_bot=False, is_webhook=False,
                                     channel_id=AUDIT_CHANNEL)

    def test_ignored_channel_and_its_threads(self):
        settings = _settings(ignored_channel_ids=[GENERAL])
        assert not core.should_store(settings, author_is_bot=False, is_webhook=False, channel_id=GENERAL)
        assert not core.should_store(settings, author_is_bot=False, is_webhook=False,
                                     channel_id="999", parent_channel_id=GENERAL)
        assert core.should_store(settings, author_is_bot=False, is_webhook=False, channel_id="333")


# --------------------------------------------------------------------------- attribution

def _delete_entry(entry_id, *, mod="900", author="500", channel=GENERAL, count=1):
    return {"id": entry_id, "user_id": mod, "target_id": author, "action_type": 72,
            "options": {"channel_id": channel, "count": str(count)}}


class TestDeleteAttribution:
    def test_self_delete_has_no_entry(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([], now=NOW)
        assert tracker.take("500", GENERAL) is None

    def test_fresh_entry_credits_the_moderator(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(_snowflake_at(NOW - 2))], now=NOW)
        assert tracker.take("500", GENERAL) == "900"
        # One deletion, one credit: the next delete by this author is theirs.
        assert tracker.take("500", GENERAL) is None

    def test_old_unseen_entry_is_not_a_credit(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(_snowflake_at(NOW - 3600), count=4)], now=NOW)
        assert tracker.take("500", GENERAL) is None

    def test_count_increase_on_known_entry_credits(self):
        entry_id = _snowflake_at(NOW - 3600)
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(entry_id, count=4)], now=NOW)
        tracker.absorb([_delete_entry(entry_id, count=6)], now=NOW + 5)
        assert tracker.take("500", GENERAL) == "900"
        assert tracker.take("500", GENERAL) == "900"
        assert tracker.take("500", GENERAL) is None

    def test_credit_matches_author_and_channel(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(_snowflake_at(NOW - 1))], now=NOW)
        assert tracker.take("501", GENERAL) is None
        assert tracker.take("500", "333") is None
        assert tracker.take("500", GENERAL) == "900"

    def test_seen_entry_without_increase_is_not_recredited(self):
        entry_id = _snowflake_at(NOW - 1)
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(entry_id)], now=NOW)
        assert tracker.take("500", GENERAL) == "900"
        tracker.absorb([_delete_entry(entry_id)], now=NOW + 3)
        assert tracker.take("500", GENERAL) is None

    def test_credits_expire(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([_delete_entry(_snowflake_at(NOW - 1))], now=NOW)
        tracker.absorb([], now=NOW + tracker.credit_ttl_seconds + 1)
        assert tracker.take("500", GENERAL) is None

    def test_malformed_entries_are_skipped(self):
        tracker = core.DeleteAttribution()
        tracker.absorb([{"nope": 1}, {"id": "x"}], now=NOW)
        assert tracker.take("500", GENERAL) is None


# --------------------------------------------------------------------------- member updates

class TestDescribeMemberUpdate:
    def test_nickname_change(self):
        entry = {"action_type": 24, "changes": [{"key": "nick", "old_value": "a", "new_value": "b"}]}
        assert core.describe_member_update(entry) == [("member_nickname", {"before": "a", "after": "b"})]

    def test_timeout_set_and_cleared(self):
        until = "2026-11-09T12:00:00+00:00"
        entry = {"action_type": 24, "changes": [{"key": "communication_disabled_until", "new_value": until}]}
        assert core.describe_member_update(entry) == [("member_timeout", {"until": until})]
        cleared = {"action_type": 24, "changes": [{"key": "communication_disabled_until", "old_value": until}]}
        assert core.describe_member_update(cleared) == [("member_timeout", {"until": None})]

    def test_role_update(self):
        entry = {"action_type": 25, "changes": [
            {"key": "$add", "new_value": [{"id": "1", "name": "Mod"}]},
            {"key": "$remove", "new_value": [{"id": "2", "name": "Old"}]},
        ]}
        kinds = core.describe_member_update(entry)
        assert kinds == [("member_roles", {"added": [{"id": "1", "name": "Mod"}],
                                           "removed": [{"id": "2", "name": "Old"}]})]

    def test_unrelated_changes_are_ignored(self):
        assert core.describe_member_update({"action_type": 24, "changes": [{"key": "deaf"}]}) == []
        assert core.describe_member_update({"action_type": 1, "changes": []}) == []


# --------------------------------------------------------------------------- embeds

def _row(**over):
    row = {
        "message_id": 123, "guild_id": int(GUILD), "channel_id": int(GENERAL), "author_id": 500,
        "author_name": "spammer", "content": "buy gold @everyone",
        "attachments": [{"filename": "scam.png", "size": 2048, "path": "g/d/x.png"}],
        "created_at": datetime.fromtimestamp(NOW - 90, tz=timezone.utc), "edited_at": None,
    }
    row.update(over)
    return row


def _field(embed, name):
    return next(f["value"] for f in embed["fields"] if f["name"] == name)


class TestEmbeds:
    def test_delete_by_moderator(self):
        embed = core.message_delete_embed(_row(), deleted_by="900", attribution_known=True, now=NOW)
        assert embed["description"] == "buy gold @everyone"
        assert _field(embed, "Deleted by") == "<@900>"
        assert "1 minute before it was deleted" in _field(embed, "Posted")
        assert "scam.png" in _field(embed, "Attachments") and "copy attached" in _field(embed, "Attachments")

    def test_delete_by_author_and_unknown(self):
        assert _field(core.message_delete_embed(_row(), deleted_by=None, attribution_known=True, now=NOW),
                      "Deleted by") == "The author"
        assert _field(core.message_delete_embed(_row(), deleted_by="500", attribution_known=True, now=NOW),
                      "Deleted by") == "The author"
        assert "Unknown" in _field(core.message_delete_embed(_row(), deleted_by=None,
                                                             attribution_known=False, now=NOW), "Deleted by")

    def test_delete_embed_fits_discord_limits(self):
        embed = core.message_delete_embed(_row(content="x" * 10000), deleted_by=None,
                                          attribution_known=True, now=NOW)
        assert len(embed["description"]) <= 4096

    def test_edit_embed(self):
        embed = core.message_edit_embed(_row(content="old"), guild_id=GUILD, channel_id=GENERAL,
                                        message_id=123, author_id=500, author_name="a", after="new", now=NOW)
        assert "**Before**\nold" in embed["description"] and "**After**\nnew" in embed["description"]
        assert embed["url"].endswith(f"/{GUILD}/{GENERAL}/123")
        unknown = core.message_edit_embed(None, guild_id=GUILD, channel_id=GENERAL, message_id=123,
                                          author_id=500, author_name=None, after="y" * 5000, now=NOW)
        assert "Not recorded" in unknown["description"]
        assert len(unknown["description"]) <= 4096

    def test_join_flags_new_accounts(self):
        new = core.member_join_embed(user_id=_snowflake_at(NOW - 3600), username="fresh", now=NOW)
        old = core.member_join_embed(user_id=_snowflake_at(NOW - 400 * 86400), username="vet", now=NOW)
        assert "new account" in new["title"]
        assert "new account" not in old["title"]

    def test_moderation_and_member_update(self):
        ban = core.moderation_embed("member_ban", target_id=500, actor_id=900, reason=None, now=NOW)
        assert ban["title"] == "Member banned" and _field(ban, "Reason") == "*None given*"
        nick = core.member_update_embed("member_nickname", {"before": None, "after": "Bob"},
                                        target_id=500, actor_id=500, now=NOW)
        assert _field(nick, "By") == "Themselves"
        timeout = core.member_update_embed("member_timeout", {"until": None}, target_id=500,
                                           actor_id=900, now=NOW)
        assert timeout["title"] == "Timeout removed"

    def test_bulk_transcript_is_oldest_first(self):
        rows = [_row(message_id=2, content="second"), _row(message_id=1, content="first")]
        text = core.bulk_delete_transcript(rows, "general")
        assert text.index("first") < text.index("second")
        assert text.startswith("Bulk delete in #general")


class TestHelpers:
    def test_human_age(self):
        assert core.human_age(1) == "1 second"
        assert core.human_age(125) == "2 minutes"
        assert core.human_age(7200) == "2 hours"
        assert core.human_age(86400) == "1 day"

    def test_clip(self):
        assert core.clip("abc", 5) == "abc"
        assert core.clip("abcdef", 5) == "ab..."
        assert core.clip(None, 5) == ""

    def test_status_text(self):
        assert "off" in core.status_text(None)
        text = core.status_text(_settings(events={"member_join": False}, ignored_channel_ids=[GENERAL]), 42)
        assert f"<#{AUDIT_CHANNEL}>" in text and "Not logged: Joins" in text
        assert f"<#{GENERAL}>" in text and "42" in text
