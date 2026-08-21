"""On-disk fallback for the intake queue (utils/webhook_spool.py).

Exists because of 2026-08-18: a full disk stopped Redis writing its RDB, MISCONF
turned that into "reject every write", and `POST /webhook` answered HTTP 200 to
~40,800 submissions it had thrown away. The plugin trusted the 200 and never
retried, so those were unrecoverable — unlike the webhook-path traffic, whose
Discord message survives.

The contract these tests pin down: a submission is either in Redis, or on disk,
or the client is told to retry. Never silently gone, and never acknowledged
before it is durable.
"""

import json
import os

import pytest

from utils import webhook_spool


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(webhook_spool, "SPOOL_DIR", str(tmp_path / "spool"))
    return webhook_spool


def _entry(guid="g1"):
    return {"payload": {"embeds": [{"guid": guid}]}, "enqueued_at": "2026-08-18T18:34:00"}


class FakeRedis:
    """Minimal rpush target. `fail` makes it behave like Redis under MISCONF."""

    def __init__(self, fail=False):
        self.pushed = []
        self.fail = fail

    def rpush(self, key, value):
        if self.fail:
            raise RuntimeError("MISCONF Redis is configured to save RDB snapshots...")
        self.pushed.append((key, value))


class TestWrite:
    def test_write_persists_and_counts(self, spool):
        assert spool.write(_entry()) is True
        assert spool.pending_count() == 1

    def test_write_creates_the_directory(self, spool):
        assert not os.path.isdir(spool.SPOOL_DIR)
        assert spool.write(_entry()) is True
        assert os.path.isdir(spool.SPOOL_DIR)

    def test_write_returns_false_instead_of_raising(self, spool, monkeypatch):
        """The caller must be able to turn failure into a retryable response."""
        monkeypatch.setattr(spool, "_ensure_dir", lambda: False)
        assert spool.write(_entry()) is False

    def test_no_partial_files_are_left_for_the_drain(self, spool):
        spool.write(_entry())
        names = os.listdir(spool.SPOOL_DIR)
        assert all(n.endswith(".json") for n in names), names

    def test_backlog_is_bounded(self, spool, monkeypatch):
        monkeypatch.setattr(spool, "MAX_SPOOL_FILES", 2)
        assert spool.write(_entry("a")) is True
        assert spool.write(_entry("b")) is True
        # At the cap it sheds rather than filling the disk Redis needs.
        assert spool.write(_entry("c")) is False


class TestDrain:
    def test_drain_requeues_and_removes(self, spool):
        spool.write(_entry("a"))
        spool.write(_entry("b"))
        r = FakeRedis()
        drained, bad = spool.drain(r, "webhook:queue")
        assert (drained, bad) == (2, 0)
        assert spool.pending_count() == 0
        assert {json.loads(v)["payload"]["embeds"][0]["guid"] for _, v in r.pushed} == {"a", "b"}

    def test_entries_survive_a_still_broken_redis(self, spool):
        spool.write(_entry("a"))
        drained, _ = spool.drain(FakeRedis(fail=True), "webhook:queue")
        assert drained == 0
        # Still on disk — losing it here would defeat the entire point.
        assert spool.pending_count() == 1

    def test_file_is_only_removed_after_a_successful_push(self, spool):
        """A crash between push and unlink must re-deliver, not lose."""
        spool.write(_entry("a"))

        class PushThenDie(FakeRedis):
            def rpush(self, key, value):
                super().rpush(key, value)
                raise KeyboardInterrupt("died after the push")

        with pytest.raises(KeyboardInterrupt):
            spool.drain(PushThenDie(), "webhook:queue")
        assert spool.pending_count() == 1

    def test_corrupt_file_is_set_aside_not_retried_forever(self, spool):
        spool.write(_entry("a"))
        name = next(n for n in os.listdir(spool.SPOOL_DIR) if n.endswith(".json"))
        with open(os.path.join(spool.SPOOL_DIR, name), "w") as fh:
            fh.write("{not json")
        drained, bad = spool.drain(FakeRedis(), "webhook:queue")
        assert (drained, bad) == (0, 1)
        assert spool.pending_count() == 0
        assert any(n.endswith(".bad") for n in os.listdir(spool.SPOOL_DIR))

    def test_drain_is_bounded_per_pass(self, spool):
        for i in range(5):
            spool.write(_entry(f"g{i}"))
        r = FakeRedis()
        drained, _ = spool.drain(r, "webhook:queue", limit=2)
        assert drained == 2
        assert spool.pending_count() == 3

    def test_drain_on_empty_spool_is_a_noop(self, spool):
        assert spool.drain(FakeRedis(), "webhook:queue") == (0, 0)


class TestRedisWrapperContract:
    """Read from the file, not via import: conftest stubs `utils` as a MagicMock,
    so `inspect.getsource` gets a mock rather than the real method. The dispatch
    parity suite scans source the same way and for the same reason."""

    def test_rpush_does_not_swallow_failures(self):
        import ast

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "utils", "redis.py",
        )
        tree = ast.parse(open(path).read())
        rpush = next(
            node
            for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == "RedisClient"
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "rpush"
        )
        handlers = [n for n in ast.walk(rpush) if isinstance(n, ast.ExceptHandler)]
        assert not handlers, (
            "RedisClient.rpush must let failures reach its caller — swallowing "
            "them is what let /webhook answer HTTP 200 for ~40,800 submissions "
            "it had discarded on 2026-08-18"
        )
