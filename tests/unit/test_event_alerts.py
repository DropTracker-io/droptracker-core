"""Unit tests for the operational-alert DM fallback (services/event_alerts.py).

The alert types here (``event_activation_failed`` / ``event_end_failed``) went
to the event's admin channel and nowhere else, so a missing channel or a bot
that couldn't post there meant nobody was told an event had failed to start.
These cover the fallback that DMs the group's leadership instead.

Loaded from the file path (the test_event_notifications idiom) so the conftest
db/services stubs don't interfere — the module is stdlib-only at import time.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    path = os.path.join(_ROOT, *relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ea = _load("_event_alerts_under_test", ("services", "event_alerts.py"))


@pytest.fixture
def real_event_notifications(monkeypatch):
    """Resolve the module's lazy ``services.event_notifications`` import to the
    real (stdlib-only) module for the duration of a test."""
    mod = _load("_event_notifications_for_alerts", ("services", "event_notifications.py"))
    monkeypatch.setitem(sys.modules, "services.event_notifications", mod)
    return mod


# ── recipient ordering (pure) ────────────────────────────────────────────────
class TestOrderRecipients:
    def test_owner_first_then_admins_then_managers_then_authed(self):
        assert ea.order_recipients(["1"], ["2", "3"], ["4"], ["5"]) == [
            "1", "2", "3", "4", "5"]

    def test_dedupes_across_buckets_keeping_highest_authority(self):
        # The owner is usually also in authed_users; they get one DM, not two.
        assert ea.order_recipients(["1"], ["2"], ["1"], ["2", "1"]) == ["1", "2"]

    def test_drops_none_and_non_numeric_ids(self):
        # A non-numeric id only produces a doomed fetch_user call downstream.
        assert ea.order_recipients([None, "abc", ""], ["7"], [], []) == ["7"]

    def test_accepts_int_ids(self):
        assert ea.order_recipients([123], [], [], []) == ["123"]

    def test_respects_the_fan_out_limit(self):
        ids = [str(i) for i in range(100, 130)]
        assert ea.order_recipients(ids, [], [], [], limit=3) == ["100", "101", "102"]

    def test_empty_when_nobody_resolves(self):
        assert ea.order_recipients([], [], [], []) == []

    def test_tolerates_none_buckets(self):
        assert ea.order_recipients(None, None, None, ["9"]) == ["9"]


# ── the "why did I get a DM" wording (pure) ──────────────────────────────────
class TestUndeliveredReason:
    def test_forbidden_names_the_permissions(self):
        text = ea.undelivered_reason_text("forbidden")
        assert "isn't allowed to post" in text
        assert "Send Messages" in text

    def test_no_channel_points_at_the_setting(self):
        assert "no Discord channel configured" in ea.undelivered_reason_text("no_channel")

    def test_unknown_code_still_explains_itself(self):
        assert "could not be posted" in ea.undelivered_reason_text("something_else")


# ── embed assembly ───────────────────────────────────────────────────────────
class TestAlertDmEmbed:
    def test_carries_the_channel_posts_content(self, real_event_notifications):
        embed = ea.alert_dm_embed(
            "event_activation_failed",
            {"event_id": 46, "event_name": "Summer's End Bingo",
             "reason": "The event needs at least one team."},
            ea.undelivered_reason_text("forbidden"),
        )
        assert "Summer's End Bingo" in embed["title"]
        # The actual failure reason is the point of the DM.
        assert "at least one team" in embed["description"]

    def test_appends_the_dm_explanation_field(self, real_event_notifications):
        embed = ea.alert_dm_embed(
            "event_activation_failed", {"event_id": 46, "event_name": "E"},
            ea.undelivered_reason_text("no_channel"),
        )
        names = [f["name"] for f in embed["fields"]]
        assert "Why this came as a DM" in names
        why = [f for f in embed["fields"] if f["name"] == "Why this came as a DM"][0]
        assert why["inline"] is False
        assert "no Discord channel configured" in why["value"]

    def test_omits_the_field_without_a_reason(self, real_event_notifications):
        embed = ea.alert_dm_embed("event_end_failed", {"event_id": 1, "event_name": "E"})
        names = [f["name"] for f in embed.get("fields", [])]
        assert "Why this came as a DM" not in names

    def test_shape_is_from_dict_compatible(self, real_event_notifications):
        # Embed.from_dict rejects a bare thumbnail/author string, so the spec's
        # own keys must not leak through untranslated.
        embed = ea.alert_dm_embed("event_activation_failed",
                                  {"event_id": 46, "event_name": "E"}, "why")
        assert set(embed) <= {"title", "description", "url", "color", "fields"}
        for field in embed.get("fields", []):
            assert set(field) == {"name", "value", "inline"}


# ── delivery ─────────────────────────────────────────────────────────────────
class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _ScriptedSession(_Session):
    """Returns queued results in call order (leadership, managers, config)."""

    def __init__(self, results):
        super().__init__()
        self._results = list(results)

    def query(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._results.pop(0)

    def first(self):
        return self._results.pop(0)


class TestAlertRecipientDiscordIds:
    def test_splits_owner_from_admins_and_appends_managers(self):
        session = _ScriptedSession([
            [("admin", "222"), ("owner", "111"), ("admin", "333")],
            [("444",)],
            None,  # no authed_users config row
        ])
        assert ea.alert_recipient_discord_ids(session, 14) == [
            "111", "222", "333", "444"]

    def test_falls_back_to_the_legacy_authed_users_config(self):
        # A group predating the web grants has no group_admins rows at all.
        session = _ScriptedSession([
            [], [],
            SimpleNamespace(config_value='["555", "666"]', long_value=None),
        ])
        assert ea.alert_recipient_discord_ids(session, 14) == ["555", "666"]

    def test_reads_authed_users_spilled_into_long_value(self):
        session = _ScriptedSession([
            [], [],
            SimpleNamespace(config_value=None, long_value='["777"]'),
        ])
        assert ea.alert_recipient_discord_ids(session, 14) == ["777"]

    def test_malformed_authed_users_does_not_lose_the_real_admins(self):
        session = _ScriptedSession([
            [("owner", "111")], [],
            SimpleNamespace(config_value="not json", long_value=None),
        ])
        assert ea.alert_recipient_discord_ids(session, 14) == ["111"]

    def test_no_group_short_circuits_before_querying(self):
        # Global events: a query here would explode on the empty script.
        assert ea.alert_recipient_discord_ids(_ScriptedSession([]), None) == []


@pytest.fixture
def outbox(monkeypatch):
    """Capture discord_outbox.enqueue calls."""
    calls = []

    def _enqueue(session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=len(calls))

    monkeypatch.setitem(sys.modules, "services.discord_outbox",
                        SimpleNamespace(enqueue=_enqueue))
    return calls


class TestEnqueueAlertDms:
    def test_ignores_non_operational_types(self, outbox):
        session = _Session()
        sent = ea.enqueue_alert_dms(session, SimpleNamespace(id=1, group_id=14),
                                    "event_started", {}, "no_channel")
        assert sent == 0
        assert outbox == []

    def test_dms_every_leader(self, monkeypatch, outbox, real_event_notifications):
        monkeypatch.setattr(ea, "alert_recipient_discord_ids",
                            lambda s, gid: ["111", "222", "333"])
        session = _Session()
        sent = ea.enqueue_alert_dms(
            session, SimpleNamespace(id=46, group_id=14, name="Summer's End"),
            "event_activation_failed", {"reason": "boom"}, "forbidden")

        assert sent == 3
        assert [c["channel_id"] for c in outbox] == ["111", "222", "333"]
        assert {c["kind"] for c in outbox} == {"dm"}
        # Rows are enqueued on the caller's session and committed once.
        assert all(c["commit"] is False for c in outbox)
        assert session.commits == 1

    def test_dm_carries_the_reason_and_a_link_button(
            self, monkeypatch, outbox, real_event_notifications):
        monkeypatch.setattr(ea, "alert_recipient_discord_ids", lambda s, gid: ["111"])
        ea.enqueue_alert_dms(
            session := _Session(),
            SimpleNamespace(id=46, group_id=14, name="Summer's End"),
            "event_activation_failed", {"reason": "no teams"}, "forbidden")

        row = outbox[0]
        assert "no teams" in row["embed"]["description"]
        assert row["components"][0]["url"].endswith("/46")
        assert row["ref_type"] == "event" and row["ref_id"] == 46
        assert session.commits == 1

    def test_no_recipients_enqueues_nothing_and_never_commits(
            self, monkeypatch, outbox):
        monkeypatch.setattr(ea, "alert_recipient_discord_ids", lambda s, gid: [])
        session = _Session()
        sent = ea.enqueue_alert_dms(
            session, SimpleNamespace(id=46, group_id=14, name="E"),
            "event_activation_failed", {"reason": "boom"}, "no_channel")
        assert sent == 0
        assert outbox == []
        assert session.commits == 0

    def test_global_event_has_no_leadership(self, outbox):
        session = _Session()
        sent = ea.enqueue_alert_dms(
            session, SimpleNamespace(id=9, group_id=None, name="Global"),
            "event_activation_failed", {"reason": "boom"}, "no_channel")
        assert sent == 0
        assert outbox == []

    def test_never_raises_when_delivery_blows_up(self, monkeypatch, outbox):
        def _boom(*a, **k):
            raise RuntimeError("outbox down")

        monkeypatch.setattr(ea, "alert_recipient_discord_ids", _boom)
        # A fallback that raises would take down the notification loop it hangs
        # off — the whole point is that it degrades quietly to a log line.
        assert ea.enqueue_alert_dms(
            _Session(), SimpleNamespace(id=46, group_id=14, name="E"),
            "event_activation_failed", {}, "forbidden") == 0
