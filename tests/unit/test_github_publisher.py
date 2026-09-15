"""Unit tests for the pure/change-gating parts of utils/github.py — the pieces
that decide WHETHER the GitHub Pages content repo gets a commit. The old
implementation committed every cycle (re-encrypted ciphertext + unconditional
dated-file writes); these tests pin the gating logic that fixed that.

Loaded standalone via importlib (conftest stubs db/utils; github + aiohttp are
real venv packages).
"""
import asyncio
import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace

import aiohttp
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


gh = _load("_github_publisher_under_test", "utils", "github.py")


class TestGitBlobSha:
    def test_matches_git_hash_object(self):
        # `printf 'hello\n' | git hash-object --stdin`
        assert gh._git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

    def test_differs_on_content_change(self):
        assert gh._git_blob_sha("1,2,3") != gh._git_blob_sha("1,2,4")


class TestStaleDatedPaths:
    PATHS = [
        "content/core.json",
        "content/news.txt",
        "content/valued_items.txt",
        "content/untradeable_items.txt",
        "content/20260722.json",
        "content/20260722-1.json",
        "content/20260722-k.txt",
        "content/20260701.json",
        "content/20260701-k.txt",
        "content/20250101.json",
    ]

    def test_only_old_dated_files_returned(self):
        stale = gh._stale_dated_paths(self.PATHS, "20260722", keep_days=7)
        assert sorted(stale) == [
            "content/20250101.json",
            "content/20260701-k.txt",
            "content/20260701.json",
        ]

    def test_non_dated_files_never_pruned(self):
        stale = gh._stale_dated_paths(self.PATHS, "20990101", keep_days=1)
        assert "content/core.json" not in stale
        assert "content/news.txt" not in stale
        assert "content/valued_items.txt" not in stale
        assert "content/untradeable_items.txt" not in stale

    def test_boundary_is_exclusive_of_keep_window(self):
        # exactly keep_days old -> kept; one day older -> pruned
        stale = gh._stale_dated_paths(
            ["content/20260715.json", "content/20260714.json"], "20260722", keep_days=7)
        assert stale == ["content/20260714.json"]


class TestWebhookSetChanged:
    def _updater(self):
        return gh.GithubPagesUpdater.__new__(gh.GithubPagesUpdater)

    def _file(self, entries):
        return SimpleNamespace(decoded_content=json.dumps(entries).encode("utf-8"))

    def test_missing_file_is_changed(self):
        assert self._updater()._webhook_set_changed(None, ["x"]) is True

    def test_same_decrypted_set_is_unchanged(self, monkeypatch):
        # Different ciphertexts (cipher-A vs cipher-B) decrypting to the same
        # urls must NOT count as a change — that was the every-cycle-commit bug.
        mapping = {"cipherA1": "url1", "cipherA2": "url2",
                   "cipherB1": "url1", "cipherB2": "url2"}
        monkeypatch.setattr(gh, "decrypt_webhook", mapping.__getitem__)
        changed = self._updater()._webhook_set_changed(
            self._file(["cipherA1", "cipherA2"]), ["cipherB2", "cipherB1"])
        assert changed is False

    def test_different_url_set_is_changed(self, monkeypatch):
        mapping = {"a": "url1", "b": "url2", "c": "url3"}
        monkeypatch.setattr(gh, "decrypt_webhook", mapping.__getitem__)
        assert self._updater()._webhook_set_changed(self._file(["a", "b"]), ["a", "c"]) is True

    def test_length_change_is_changed(self, monkeypatch):
        monkeypatch.setattr(gh, "decrypt_webhook", lambda entry: entry)
        assert self._updater()._webhook_set_changed(self._file(["a"]), ["a", "b"]) is True

    def test_undecryptable_existing_content_is_changed(self, monkeypatch):
        def _boom(entry):
            raise ValueError("bad token")
        monkeypatch.setattr(gh, "decrypt_webhook", _boom)
        assert self._updater()._webhook_set_changed(self._file(["a"]), ["a"]) is True

    def test_non_json_existing_content_is_changed(self):
        bad = SimpleNamespace(decoded_content=b"<html>error</html>")
        assert self._updater()._webhook_set_changed(bad, ["a"]) is True


class TestSummarizePublish:
    """The change lines fed to the Discord automation channel. Empty list ==
    the run changed nothing and only the status message gets refreshed."""

    def test_no_changes_is_empty(self):
        assert gh.summarize_publish([], [], 0) == []
        assert gh.summarize_publish([], [], 0, {"tested": 120, "deleted": 0}) == []
        assert gh.summarize_publish([], [], 0, None) == []

    def test_full_run_produces_all_lines(self):
        files = [("content/core.json", "x"), ("content/news.txt", "y")]
        lines = gh.summarize_publish(files, ["content/20260701.json"], 1,
                                     {"tested": 120, "deleted": 5})
        assert lines == [
            "Committed 2 file(s): core.json, news.txt",
            "1 webhook file(s) rotated",
            "Pruned 1 stale dated file(s)",
            "Deleted 5 confirmed-dead webhook(s) (of 120 tested)",
        ]

    def test_filename_enumeration_capped(self):
        files = [(f"content/f{i}.txt", "x") for i in range(9)]
        lines = gh.summarize_publish(files, [], 0)
        assert lines[0].startswith("Committed 9 file(s): ")
        assert "+3 more" in lines[0]
        assert "f6.txt" not in lines[0]

    def test_dead_webhook_line_only_when_deleted(self):
        assert gh.summarize_publish([], [], 0, {"tested": 120, "deleted": 0}) == []
        lines = gh.summarize_publish([], [], 0, {"tested": 80, "deleted": 2})
        assert lines == ["Deleted 2 confirmed-dead webhook(s) (of 80 tested)"]

    def test_untrusted_run_is_reported(self):
        lines = gh.summarize_publish([], [], 0, {"tested": 120, "inconclusive": 120, "degraded": True})
        assert len(lines) == 1
        assert "120 of 120 probes inconclusive" in lines[0]
        assert "nothing deleted" in lines[0]

    def test_new_strikes_are_reported(self):
        lines = gh.summarize_publish([], [], 0, {"tested": 180, "struck": 3})
        assert lines == ["Flagged 3 webhook(s) answering Unknown Webhook: unpublished, deleted if still dead after 6h"]


class TestItemListContents:
    """valued_items.txt must come from the shared resolver so name-only
    override rows (item_id NULL, matched by name at intake) reach the plugin's
    force-screenshot list — the 2026-08-03 Elder venator fang incident."""

    def test_valued_list_uses_shared_resolver(self, monkeypatch):
        import utils.value_overrides as vo

        monkeypatch.setattr(vo, "active_item_ids", lambda: [28319, 33634])
        updater = gh.GithubPagesUpdater.__new__(gh.GithubPagesUpdater)
        contents = dict(updater._item_list_contents())
        assert contents["content/valued_items.txt"] == "28319,33634"

    def test_empty_resolver_result_publishes_nothing(self, monkeypatch):
        # An empty id list (e.g. DB unreachable) must not overwrite the
        # published file with a blank one.
        import utils.value_overrides as vo

        monkeypatch.setattr(vo, "active_item_ids", lambda: [])
        updater = gh.GithubPagesUpdater.__new__(gh.GithubPagesUpdater)
        contents = dict(updater._item_list_contents())
        assert "content/valued_items.txt" not in contents


# --- Webhook liveness (2026-09-15: the old check deleted every published
# webhook during a Discord incident; all of them were alive) ------------------


def _row(row_id):
    webhook_id = str(1377677000000000000 + row_id)
    return gh.PoolRow(row_id, webhook_id, f"https://discord.com/api/webhooks/{webhook_id}/token-{row_id}")


class TestClassifyProbe:
    def test_the_webhook_itself_is_alive(self):
        assert gh.classify_probe(200, {"id": "123", "token": "t"}, "123") == gh.ALIVE

    def test_a_200_that_is_not_the_webhook_is_inconclusive(self):
        assert gh.classify_probe(200, None, "123") == gh.INCONCLUSIVE
        assert gh.classify_probe(200, {"id": "999"}, "123") == gh.INCONCLUSIVE

    def test_only_unknown_webhook_is_dead(self):
        assert gh.classify_probe(404, {"message": "Unknown Webhook", "code": 10015}, "123") == gh.DEAD

    @pytest.mark.parametrize("status,payload", [
        (404, None),
        (404, {"message": "404: Not Found", "code": 0}),
        (401, {"message": "Invalid Webhook Token", "code": 50027}),
        (403, None),
        (429, {"message": "You are being rate limited.", "retry_after": 1.2}),
        (500, None),
        (502, {"message": "Bad Gateway"}),
        (503, None),
        (None, None),
    ])
    def test_everything_else_is_inconclusive(self, status, payload):
        assert gh.classify_probe(status, payload, "123") == gh.INCONCLUSIVE


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Raises:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _Http:
    def __init__(self, context):
        self._context = context

    def get(self, url, timeout=None):
        return self._context


class TestProbeWebhook:
    URL = "https://discord.com/api/webhooks/1377677447021199371/abc-DEF_123"

    def _probe(self, context):
        return asyncio.run(gh.probe_webhook(_Http(context), self.URL))

    def test_alive_checks_the_id_from_the_url(self):
        assert self._probe(_Response(200, {"id": "1377677447021199371"})) == (gh.ALIVE, "200")

    def test_unknown_webhook(self):
        assert self._probe(_Response(404, {"code": 10015})) == (gh.DEAD, "404")

    def test_unparseable_body_is_inconclusive(self):
        assert self._probe(_Response(404, ValueError("html"))) == (gh.INCONCLUSIVE, "404")

    def test_timeout_does_not_escape(self):
        assert self._probe(_Raises(asyncio.TimeoutError())) == (gh.INCONCLUSIVE, "TimeoutError")

    def test_connection_error_does_not_escape(self):
        verdict, detail = self._probe(_Raises(aiohttp.ServerDisconnectedError()))
        assert (verdict, detail) == (gh.INCONCLUSIVE, "ServerDisconnectedError")


class TestRunIsDegraded:
    def test_a_few_blips_are_tolerated(self):
        assert gh.run_is_degraded(180, 18) is False

    def test_an_outage_is_untrusted(self):
        assert gh.run_is_degraded(120, 120) is True
        assert gh.run_is_degraded(180, 19) is True

    def test_small_runs_use_the_floor(self):
        assert gh.run_is_degraded(10, 5) is False
        assert gh.run_is_degraded(10, 6) is True


class TestPlanStrikes:
    NOW = 2_000_000_000.0

    def test_first_dead_answer_only_strikes(self):
        assert gh.plan_strikes({1: gh.DEAD}, {}, self.NOW) == ([], {1: self.NOW}, [])

    def test_still_dead_after_the_window_deletes(self):
        struck = {1: self.NOW - gh.DEAD_CONFIRM_SECONDS}
        assert gh.plan_strikes({1: gh.DEAD}, struck, self.NOW) == ([1], {}, [])

    def test_still_dead_inside_the_window_waits(self):
        struck = {1: self.NOW - gh.DEAD_CONFIRM_SECONDS + 60}
        assert gh.plan_strikes({1: gh.DEAD}, struck, self.NOW) == ([], {}, [])

    def test_alive_clears_the_strike(self):
        assert gh.plan_strikes({1: gh.ALIVE}, {1: self.NOW - 10}, self.NOW) == ([], {}, [1])

    def test_inconclusive_changes_nothing(self):
        old = self.NOW - 2 * gh.DEAD_CONFIRM_SECONDS
        assert gh.plan_strikes({1: gh.INCONCLUSIVE, 2: gh.INCONCLUSIVE}, {1: old}, self.NOW) == ([], {}, [])


class TestRotationSlice:
    ROWS = [_row(i) for i in range(1, 11)]

    def test_starts_at_the_top(self):
        picked, cursor = gh.rotation_slice(self.ROWS, None, set(), batch=3)
        assert [r.id for r in picked] == [1, 2, 3]
        assert cursor == gh.pool_sort_key(self.ROWS[2])

    def test_continues_after_the_cursor_skipping_excluded(self):
        picked, _ = gh.rotation_slice(self.ROWS, gh.pool_sort_key(self.ROWS[2]), {4, 5}, batch=3)
        assert [r.id for r in picked] == [6, 7, 8]

    def test_wraps_around(self):
        picked, cursor = gh.rotation_slice(self.ROWS, gh.pool_sort_key(self.ROWS[8]), set(), batch=3)
        assert [r.id for r in picked] == [10, 1, 2]
        assert cursor == gh.pool_sort_key(self.ROWS[1])

    def test_cursor_row_deleted_since(self):
        rows = [r for r in self.ROWS if r.id != 3]
        picked, _ = gh.rotation_slice(rows, gh.pool_sort_key(self.ROWS[2]), set(), batch=2)
        assert [r.id for r in picked] == [4, 5]

    def test_everything_excluded_keeps_the_cursor(self):
        picked, cursor = gh.rotation_slice(self.ROWS, ("5", 5), {r.id for r in self.ROWS}, batch=3)
        assert picked == [] and cursor == ("5", 5)


class _Redis:
    def __init__(self):
        self.hash, self.kv = {}, {}

    def hgetall(self, key):
        assert key == gh.DEAD_STRIKES_KEY
        return dict(self.hash)

    def hset(self, key, mapping):
        self.hash.update({str(k).encode(): str(v).encode() for k, v in mapping.items()})

    def hdel(self, key, *fields):
        for field in fields:
            self.hash.pop(str(field).encode(), None)

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value.encode()

    def strike(self, row_id, at):
        self.hash[str(row_id).encode()] = str(at).encode()

    def strikes(self):
        return {int(k): float(v) for k, v in self.hash.items()}


@pytest.fixture
def pool(monkeypatch):
    """check_pool_webhooks wired to in-memory rows, answers and Redis."""
    state = SimpleNamespace(rows=[], answers={}, redis=_Redis(), deleted=[], probed=[])
    monkeypatch.setattr(gh, "PROBE_DELAY_SECONDS", 0)
    monkeypatch.setattr(gh, "_load_pool_rows", lambda: sorted(state.rows, key=gh.pool_sort_key))
    monkeypatch.setattr(gh, "_redis_conn", lambda: state.redis)

    async def probe(_http, url):
        state.probed.append(url)
        return state.answers.get(url, (gh.ALIVE, "200"))

    def delete(rows):
        state.deleted.extend(rows)
        return len(rows)

    monkeypatch.setattr(gh, "probe_webhook", probe)
    monkeypatch.setattr(gh, "_delete_rows", delete)
    state.run = lambda: asyncio.run(gh.check_pool_webhooks())
    return state


class TestCheckPoolWebhooks:
    def test_an_outage_deletes_nothing_and_leaves_the_files(self, pool):
        # The 2026-09-15 14:44 run: every probe failed while every webhook was alive.
        pool.rows = [_row(i) for i in range(1, 121)]
        pool.answers = {row.url: (gh.INCONCLUSIVE, "503") for row in pool.rows}
        report = pool.run()
        assert report["degraded"] is True
        assert report["publish_urls"] is None
        assert report["deleted"] == 0 and pool.deleted == []
        assert pool.redis.strikes() == {}

    def test_an_unknown_webhook_storm_only_strikes(self, pool):
        pool.rows = [_row(i) for i in range(1, 41)]
        pool.answers = {row.url: (gh.DEAD, "404") for row in pool.rows}
        report = pool.run()
        assert report["deleted"] == 0 and pool.deleted == []
        assert report["struck"] == 40
        assert report["publish_urls"] == []
        assert set(pool.redis.strikes()) == set(range(1, 41))

    def test_confirmed_dead_row_is_deleted(self, pool):
        pool.rows = [_row(i) for i in range(1, 6)]
        pool.answers = {pool.rows[1].url: (gh.DEAD, "404")}
        pool.redis.strike(2, time.time() - gh.DEAD_CONFIRM_SECONDS - 60)
        report = pool.run()
        assert [row.id for row in pool.deleted] == [2]
        assert report["deleted"] == 1 and report["struck"] == 0
        assert pool.rows[1].url not in report["publish_urls"]
        assert 2 not in pool.redis.strikes()

    def test_alive_answer_clears_an_old_strike(self, pool):
        pool.rows = [_row(1), _row(2)]
        pool.redis.strike(1, time.time() - 30)
        report = pool.run()
        assert pool.redis.strikes() == {}
        assert pool.rows[0].url in report["publish_urls"]

    def test_struck_row_stays_unpublished_while_inconclusive(self, pool):
        pool.rows = [_row(1), _row(2)]
        pool.answers = {pool.rows[0].url: (gh.INCONCLUSIVE, "TimeoutError")}
        pool.redis.strike(1, time.time() - 30)
        report = pool.run()
        assert report["publish_urls"] == [pool.rows[1].url]
        assert 1 in pool.redis.strikes()

    def test_publish_walk_skips_dead_rows_to_fill_the_set(self, pool, monkeypatch):
        monkeypatch.setattr(gh, "PUBLISH_COUNT", 3)
        pool.rows = [_row(i) for i in range(1, 7)]
        pool.answers = {pool.rows[1].url: (gh.DEAD, "404"), pool.rows[2].url: (gh.DEAD, "404")}
        report = pool.run()
        assert report["publish_urls"] == [pool.rows[0].url, pool.rows[3].url, pool.rows[4].url]
        assert report["struck"] == 2

    def test_rotation_reaches_rows_behind_the_published_ones(self, pool, monkeypatch):
        monkeypatch.setattr(gh, "PUBLISH_COUNT", 2)
        monkeypatch.setattr(gh, "ROTATION_BATCH", 2)
        pool.rows = [_row(i) for i in range(1, 9)]
        pool.answers = {pool.rows[5].url: (gh.DEAD, "404")}
        pool.run()
        assert pool.probed == [pool.rows[i].url for i in (0, 1, 2, 3)]
        pool.probed.clear()
        report = pool.run()
        assert pool.probed == [pool.rows[i].url for i in (0, 1, 4, 5)]
        assert report["struck"] == 1 and 6 in pool.redis.strikes()

    def test_without_redis_nothing_is_deleted(self, pool, monkeypatch):
        monkeypatch.setattr(gh, "_redis_conn", lambda: None)
        pool.rows = [_row(1), _row(2), _row(3)]
        pool.answers = {pool.rows[1].url: (gh.DEAD, "404")}
        report = pool.run()
        assert report["deleted"] == 0 and report["struck"] == 0
        assert report["publish_urls"] == [pool.rows[0].url, pool.rows[2].url]

    def test_strikes_for_rows_no_longer_in_the_table_are_dropped(self, pool):
        pool.rows = [_row(1)]
        pool.redis.strike(99, time.time() - 30)
        pool.run()
        assert pool.redis.strikes() == {}


class TestUpdateGithubPagesWebhookGate:
    def _updater(self, monkeypatch):
        updater = gh.GithubPagesUpdater.__new__(gh.GithubPagesUpdater)
        updater.branch = "main"
        updater.repo = SimpleNamespace(get_contents=lambda path, ref=None: [])
        monkeypatch.setattr(updater, "_prepare_news_update", lambda listing=None: ("content/news.txt", "hi"))
        monkeypatch.setattr(updater, "_prepare_encryption_key_update", lambda listing=None: None)
        monkeypatch.setattr(updater, "_item_list_contents", lambda: [])
        commits = []
        monkeypatch.setattr(updater, "update_multiple_files",
                            lambda files, commit_message, branch, deletions: commits.append(dict(files)))
        return updater, commits

    def test_untrusted_check_leaves_webhook_files_but_publishes_the_rest(self, monkeypatch):
        updater, commits = self._updater(monkeypatch)

        def must_not_encrypt(**kwargs):
            raise AssertionError("webhook files must not be rebuilt")

        monkeypatch.setattr(updater, "fetch_webhooks_from_database", must_not_encrypt)
        updater._update_github_pages(None)
        assert list(commits[0]) == ["content/news.txt"]

    def test_trusted_check_publishes_the_given_urls_in_order(self, monkeypatch):
        updater, commits = self._updater(monkeypatch)
        urls = [f"url-{i}" for i in range(120)]
        monkeypatch.setattr(updater, "fetch_webhooks_from_database",
                            lambda limit=120, urls=None: [f"enc-{u}" for u in urls])
        updater._update_github_pages(urls)
        assert json.loads(commits[0]["content/core.json"]) == [f"enc-url-{i}" for i in range(40)]
