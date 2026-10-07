"""Clan-leader updates (services/leader_updates.py) and the outbox news_post kind.

What matters here:

* **Pilot first.** Until LEADER_UPDATES_LIVE is on, a global announcement is
  DMed to the pilot accounts and never queued for the news channel, and the
  role sweep only considers pilot accounts.
* **An opt-out sticks.** Leaders who turned the role off are never candidates.
* **Each leader is looked up once.** Handled leaders and recently-absent ones
  are skipped, and a sweep is capped.
* **Only an approver publishes a site-wide post.** Everything else is a draft
  that DMs the approver; approving publishes, and only then does Discord get
  it. The owner is user_id 0, which must never be truthiness-tested away.
* **news_post publishes.** The drain sends, then publishes to followers; a
  failed publish leaves the row sent with a note, never failed or resent.
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lu = _load("_leader_updates_under_test", "services", "leader_updates.py")
outbox = _load("_outbox_news_under_test", "services", "discord_outbox.py")

OWNER = "528746710042804247"


class _Env:
    """Patch the two env switches for one test."""

    def __init__(self, live=None, pilot=None):
        values = {}
        if live is not None:
            values[lu.ENV_LIVE] = live
        if pilot is not None:
            values[lu.ENV_PILOT_IDS] = pilot
        self._patch = patch.dict(os.environ, values, clear=False)
        self._clear = [k for k in (lu.ENV_LIVE, lu.ENV_PILOT_IDS) if k not in values]

    def __enter__(self):
        self._patch.__enter__()
        self._saved = {k: os.environ.pop(k) for k in self._clear if k in os.environ}
        return self

    def __exit__(self, *exc):
        os.environ.update(self._saved)
        return self._patch.__exit__(*exc)


class TestSwitches(unittest.TestCase):
    def test_pilot_by_default_with_the_owner(self):
        with _Env():
            self.assertFalse(lu.is_live())
            self.assertEqual(lu.pilot_discord_ids(), {OWNER})
            self.assertTrue(lu.in_audience(OWNER))
            self.assertFalse(lu.in_audience("123"))

    def test_live_reaches_everyone(self):
        with _Env(live="true"):
            self.assertTrue(lu.is_live())
            self.assertTrue(lu.in_audience("123"))

    def test_pilot_list_from_env_ignores_junk(self):
        with _Env(pilot=" 111, abc ,222,"):
            self.assertEqual(lu.pilot_discord_ids(), {"111", "222"})

    def test_user_id_zero_style_ids_are_not_dropped(self):
        # in_audience must not truthiness-test: "0" is a real string id.
        with _Env(live="1"):
            self.assertTrue(lu.in_audience(0))
        self.assertFalse(lu.in_audience(None))


class TestCandidates(unittest.TestCase):
    def test_pilot_limits_and_opt_outs_apply(self):
        with _Env(pilot=f"{OWNER},999"), \
                patch.object(lu, "leader_discord_ids", return_value={OWNER, "999", "555"}), \
                patch.object(lu, "opted_out_ids", side_effect=lambda s, ids: {"999"} & set(ids)):
            self.assertEqual(lu.role_grant_candidates(None), [OWNER])

    def test_live_covers_every_leader_but_opted_out(self):
        with _Env(live="true"), \
                patch.object(lu, "leader_discord_ids", return_value={"3", "1", "2"}), \
                patch.object(lu, "opted_out_ids", return_value={"2"}):
            self.assertEqual(lu.role_grant_candidates(None), ["1", "3"])


class _FakeRedis:
    def __init__(self, granted=(), absent=()):
        self.granted = {g.encode() for g in granted}
        self.absent = set(absent)

    def smembers(self, key):
        return set(self.granted)

    def pipeline(self):
        conn = self
        calls = []

        class _Pipe:
            def exists(self, key):
                calls.append(key[len(lu.ABSENT_KEY_PREFIX):] in conn.absent)

            def execute(self):
                return list(calls)

        return _Pipe()


class TestPendingGrants(unittest.TestCase):
    def test_skips_handled_and_recently_absent(self):
        conn = _FakeRedis(granted=["1"], absent=["2"])
        with patch.object(lu, "role_grant_candidates", return_value=["1", "2", "3", "4"]):
            self.assertEqual(lu.pending_role_grants(None, conn), ["3", "4"])

    def test_capped(self):
        with patch.object(lu, "role_grant_candidates", return_value=[str(i) for i in range(100)]):
            self.assertEqual(len(lu.pending_role_grants(None, _FakeRedis(), cap=40)), 40)

    def test_no_redis_means_no_lookups(self):
        with patch.object(lu, "role_grant_candidates", return_value=["1"]):
            self.assertEqual(lu.pending_role_grants(None, None), [])


class TestGlobalAnnouncement(unittest.TestCase):
    def _run(self, **env):
        enqueue = MagicMock(side_effect=lambda s, **kw: kw)
        fake = types.SimpleNamespace(enqueue=enqueue)
        with _Env(**env), patch.dict(sys.modules, {"services.discord_outbox": fake}):
            rows = lu.enqueue_global_announcement(
                MagicMock(), ann_id=7, title="Big change", body_md="x" * 5000, actor_user_id=0,
            )
        return rows

    def test_pilot_dms_the_pilot_accounts_only(self):
        rows = self._run(pilot=f"{OWNER},111")
        self.assertEqual(sorted(r["channel_id"] for r in rows), ["111", OWNER])
        for r in rows:
            self.assertEqual(r["kind"], "dm")
            self.assertNotEqual(r["channel_id"], str(lu.NEWS_CHANNEL_ID))
            self.assertIn("Pilot preview", r["content"])
            self.assertEqual(len(r["embed"]["description"]), 4000)
            self.assertTrue(r["components"][0]["url"].endswith("/announcements/7"))

    def test_live_posts_to_the_news_channel_for_publishing(self):
        rows = self._run(live="true")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "news_post")
        self.assertEqual(rows[0]["channel_id"], str(lu.NEWS_CHANNEL_ID))
        self.assertEqual(rows[0]["ref_type"], "announcement")
        self.assertIsNone(rows[0].get("content"))


class _Col:
    """Stands in for a model column under the stubbed ``db`` package."""

    def __eq__(self, other):
        return True

    __lt__ = __eq__
    __hash__ = object.__hash__

    def asc(self):
        return self


_FakeOutboxModel = types.SimpleNamespace(status=_Col(), created_at=_Col())


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    order_by = limit = with_for_update = filter

    def update(self, *a, **k):
        return 0

    def all(self):
        return self._rows

    def first(self):
        return None


def _news_row():
    row = MagicMock()
    row.id = 1
    row.kind = "news_post"
    row.channel_id = "1527845346582073426"
    row.content = "hello"
    row.embed_json = None
    row.components_json = None
    row.ref_type = "announcement"
    row.ref_id = 7
    row.error = None
    return row


class TestNewsPostDrain(unittest.TestCase):
    def _drain(self, message):
        row = _news_row()
        session = MagicMock()
        session.query.return_value = _Query([row])
        channel = MagicMock()
        channel.send = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        with patch.object(outbox, "_allowed_mentions_for", return_value=None), \
                patch.object(outbox, "DiscordOutbox", _FakeOutboxModel):
            sent = asyncio.run(outbox.drain_once(bot, lambda: session))
        return sent, row

    def test_posts_then_publishes(self):
        message = MagicMock(id=42)
        message.publish = AsyncMock()
        sent, row = self._drain(message)
        self.assertEqual(sent, 1)
        message.publish.assert_awaited_once()
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.discord_message_id, "42")

    def test_failed_publish_is_noted_not_failed(self):
        message = MagicMock(id=43)
        message.publish = AsyncMock(side_effect=RuntimeError("missing perms"))
        sent, row = self._drain(message)
        self.assertEqual(row.status, "sent")
        self.assertIn("not published", row.error)

    def test_plain_announcement_is_not_published(self):
        message = MagicMock(id=44)
        message.publish = AsyncMock()
        row = _news_row()
        row.kind = "announcement"
        session = MagicMock()
        session.query.return_value = _Query([row])
        channel = MagicMock()
        channel.send = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        with patch.object(outbox, "_allowed_mentions_for", return_value=None), \
                patch.object(outbox, "DiscordOutbox", _FakeOutboxModel):
            asyncio.run(outbox.drain_once(bot, lambda: session))
        channel.send.assert_awaited_once()
        message.publish.assert_not_awaited()


class TestApprovers(unittest.TestCase):
    def test_owner_user_zero_is_the_default_approver(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lu.ENV_APPROVER_USER_IDS, None)
            self.assertTrue(lu.is_approver(0))
            self.assertFalse(lu.is_approver(None))
            self.assertFalse(lu.is_approver(5))

    def test_env_list(self):
        with patch.dict(os.environ, {lu.ENV_APPROVER_USER_IDS: "0, 12,junk"}):
            self.assertEqual(lu.approver_user_ids(), {0, 12})
            self.assertTrue(lu.is_approver("12"))


class _Ann:
    def __init__(self, **kw):
        self.id = None
        self.__dict__.update(kw)


class TestReviewDraft(unittest.TestCase):
    def _session(self):
        session = MagicMock()

        def _flush():
            for call in session.add.call_args_list:
                call.args[0].id = 99

        session.flush.side_effect = _flush
        return session

    def test_draft_is_private_and_dms_the_approver(self):
        session = self._session()
        fake_models = types.SimpleNamespace(Announcement=_Ann)
        enqueue = MagicMock()
        with patch.dict(sys.modules, {"db.models": fake_models,
                                      "services.discord_outbox": types.SimpleNamespace(enqueue=enqueue)}), \
                patch.object(lu, "_approver_discord_ids", return_value=[OWNER]):
            ann = lu.create_review_draft(
                session, title="Roundup", body_md="b" * 3000, post_to_discord=True,
                source_label="weekly roundup",
            )
        self.assertEqual(ann.status, "draft")
        self.assertIsNone(ann.published_at)
        self.assertTrue(ann.post_to_discord)
        enqueue.assert_called_once()
        kw = enqueue.call_args.kwargs
        self.assertEqual(kw["kind"], "dm")
        self.assertEqual(kw["channel_id"], OWNER)
        self.assertEqual(kw["ref_id"], 99)
        self.assertIn("Nothing goes out until you approve it", kw["content"])
        self.assertLess(len(kw["embed"]["description"]), 1600)
        session.commit.assert_called()

    def test_publish_reviewed_sends_to_discord_only_when_asked(self):
        for post, expected in ((True, 1), (False, 0)):
            ann = _Ann(id=5, title="t", body_md="b", status="draft", post_to_discord=post)
            with patch.object(lu, "enqueue_global_announcement") as eg:
                lu.publish_reviewed(MagicMock(), ann, 0)
            self.assertEqual(ann.status, "published")
            self.assertEqual(ann.reviewed_by, 0)
            self.assertIsNotNone(ann.published_at)
            self.assertEqual(eg.call_count, expected)


if __name__ == "__main__":
    unittest.main()
