"""Unit tests for undoing a manual-submission review decision.

``services/drop_moderation.py`` is loaded from its file path (same pattern as
test_manual_policy.py) so the conftest ``services`` MagicMock never shadows it.
Its DB/Redis/outbox dependencies are all function-local imports, so each test
injects exactly the fakes the path under test touches.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULE_PATH = os.path.join(_ROOT, "services", "drop_moderation.py")
_spec = importlib.util.spec_from_file_location("_drop_moderation_under_test", _MODULE_PATH)
dm = importlib.util.module_from_spec(_spec)
sys.modules["_drop_moderation_under_test"] = dm
_spec.loader.exec_module(dm)


# ── Fakes ─────────────────────────────────────────────────────────────────────
class _AnyAttr(type):
    """Metaclass letting a fake model answer any column lookup, so the module's
    ``Model.column == value`` filter expressions evaluate harmlessly."""

    def __getattr__(cls, name):
        return object()


class FakeNotificationQueue(metaclass=_AnyAttr):
    pass


class FakeNotifiedSubmission(metaclass=_AnyAttr):
    pass


class FakeDrop(metaclass=_AnyAttr):
    pass


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Dispatches ``query(Model)`` to a canned row list and records deletes."""

    def __init__(self, rows_by_model=None):
        self.rows_by_model = rows_by_model or {}
        self.deleted = []
        self.flushed = 0

    def query(self, model, *_rest):
        return FakeQuery(self.rows_by_model.get(model, []))

    def delete(self, row):
        self.deleted.append(row)

    def flush(self):
        self.flushed += 1


class FakeRedis:
    """Just enough sorted-set behaviour to assert the debit arithmetic."""

    def __init__(self, scores=None):
        self.scores = dict(scores or {})   # (key, member) -> score
        self.expires = []

    def zscore(self, key, member):
        return self.scores.get((key, str(member)))

    def zincrby(self, key, amount, member):
        k = (key, str(member))
        self.scores[k] = self.scores.get(k, 0) + amount
        return self.scores[k]

    def zadd(self, key, mapping):
        for member, score in mapping.items():
            self.scores[(key, str(member))] = score

    def expire(self, key, ttl):
        self.expires.append((key, ttl))

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    """Buffers commands and replays them on the connection at execute()."""

    def __init__(self, conn):
        self.conn = conn
        self.queued = []

    def zincrby(self, key, amount, member):
        self.queued.append(("zincrby", (key, amount, member)))

    def expire(self, key, ttl):
        self.queued.append(("expire", (key, ttl)))

    def execute(self):
        return [getattr(self.conn, op)(*args) for op, args in self.queued]


@pytest.fixture
def redis(monkeypatch):
    conn = FakeRedis()
    fake_module = ModuleType("utils.redis")
    fake_module.redis_client = SimpleNamespace(client=conn)
    monkeypatch.setitem(sys.modules, "utils.redis", fake_module)
    return conn


@pytest.fixture
def models(monkeypatch):
    """Minimal ``db.models`` surface for the undo path. Real tuples, not mock
    attributes — a MagicMock ``in`` check silently returns False and would make
    every status look un-undoable."""
    fake = ModuleType("db.models")
    fake.Drop = FakeDrop
    fake.NotificationQueue = FakeNotificationQueue
    fake.NotifiedSubmission = FakeNotifiedSubmission
    fake.UNDOABLE_STATUSES = ("approved", "rejected")
    fake.EXCLUDING_STATUSES = ("excluded", "pending", "rejected")
    fake.COUNTING_STATUSES = ("approved",)
    monkeypatch.setitem(sys.modules, "db.models", fake)
    return fake


@pytest.fixture
def outbox(monkeypatch):
    """Captures ``discord_outbox.enqueue`` calls."""
    calls = []
    fake = ModuleType("services.discord_outbox")

    def enqueue(session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    fake.enqueue = enqueue
    monkeypatch.setitem(sys.modules, "services.discord_outbox", fake)
    return calls


def _mod_row(status="approved", **kwargs):
    return SimpleNamespace(
        status=status,
        reviewed_by_user_id=99,
        reviewed_at=datetime(2026, 8, 1, 12, 0),
        reason="policy:confirm",
        **kwargs,
    )


def _drop(value=1_000_000, quantity=2, dt=None):
    return SimpleNamespace(
        drop_id=555, player_id=42, value=value, quantity=quantity,
        date_added=dt or datetime.now(), image_url=None, item_id=1, npc_id=2,
    )


# ── Transition guards ─────────────────────────────────────────────────────────
class TestUndoGuards:
    def test_missing_row_is_404(self, models, monkeypatch):
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: None)
        with pytest.raises(dm.ModerationError) as e:
            dm.undo_review_for_group(FakeSession(), 555, 7)
        assert e.value.status == 404

    def test_pending_row_has_nothing_to_undo(self, models, monkeypatch):
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: _mod_row("pending"))
        with pytest.raises(dm.ModerationError) as e:
            dm.undo_review_for_group(FakeSession(), 555, 7)
        assert e.value.status == 409
        assert e.value.title == "Nothing to undo"

    def test_policy_excluded_row_is_not_undoable(self, models, monkeypatch):
        # 'excluded' (block / authorized_only) was never a reviewer's decision,
        # so there is nothing to take back — changing the policy is the way out.
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: _mod_row("excluded"))
        with pytest.raises(dm.ModerationError) as e:
            dm.undo_review_for_group(FakeSession(), 555, 7)
        assert e.value.status == 409
        assert e.value.title == "Not reviewable"


# ── The undo itself ───────────────────────────────────────────────────────────
class TestUndoReview:
    def test_undoing_a_rejection_is_a_plain_status_flip(self, models, monkeypatch):
        row = _mod_row("rejected")
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: row)
        debits, retracts = [], []
        monkeypatch.setattr(dm, "_debit_group_boards", lambda *a: debits.append(a))
        monkeypatch.setattr(dm, "_retract_group_notification", lambda *a: retracts.append(a))

        result = dm.undo_review_for_group(FakeSession(), 555, 7, reviewer_user_id=3)

        assert row.status == "pending"
        assert result["previous_status"] == "rejected"
        assert result["debited"] == 0
        # Rejected never counted, so there is nothing to reverse.
        assert debits == [] and retracts == []

    def test_undo_clears_the_review_stamp(self, models, monkeypatch):
        row = _mod_row("rejected")
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: row)
        dm.undo_review_for_group(FakeSession(), 555, 7, reviewer_user_id=3)
        # The row genuinely has no decision on it again; who undid it lives in
        # the audit log, not here.
        assert row.reviewed_by_user_id is None
        assert row.reviewed_at is None

    def test_undoing_an_approval_debits_and_retracts(self, models, monkeypatch):
        row = _mod_row("approved")
        drop = _drop(value=1_000_000, quantity=2)
        session = FakeSession({FakeDrop: [drop]})
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: row)
        debits = []
        monkeypatch.setattr(dm, "_debit_group_boards", lambda *a: debits.append(a))
        monkeypatch.setattr(
            dm, "_retract_group_notification",
            lambda *a: {"dequeued": 1, "deleted": 0},
        )

        result = dm.undo_review_for_group(session, 555, 7, reviewer_user_id=3)

        assert row.status == "pending"
        assert debits == [(42, 7, 2_000_000, drop.date_added)]
        assert result == {
            "drop_id": 555, "group_id": 7, "status": "pending",
            "previous_status": "approved", "debited": 2_000_000,
            "notification_dequeued": 1, "notification_deleted": 0,
        }

    def test_board_failure_does_not_block_the_status_flip(self, models, monkeypatch):
        # Redis being down must not leave the row stuck as approved — the
        # status is what every read-path rebuild keys off.
        row = _mod_row("approved")
        session = FakeSession({FakeDrop: [_drop()]})
        monkeypatch.setattr(dm, "_load_moderation_row", lambda *a: row)

        def boom(*_a):
            raise RuntimeError("redis down")

        monkeypatch.setattr(dm, "_debit_group_boards", boom)
        monkeypatch.setattr(dm, "_retract_group_notification", boom)

        result = dm.undo_review_for_group(session, 555, 7)
        assert row.status == "pending"
        assert result["status"] == "pending"


# ── Board arithmetic ──────────────────────────────────────────────────────────
class TestDebitGroupBoards:
    def test_debit_reverses_a_credit_exactly(self, redis):
        dt = datetime.now()
        dm._credit_group_boards(42, 7, 5_000, dt)
        credited = dict(redis.scores)
        assert credited, "credit should have written boards"
        dm._debit_group_boards(42, 7, 5_000, dt)
        assert all(score == 0 for score in redis.scores.values())
        assert set(redis.scores) == set(credited)

    def test_debit_never_creates_a_missing_board(self, redis):
        # An expired daily board must not be resurrected holding a negative
        # score — skip anything the player has no entry on.
        dm._debit_group_boards(42, 7, 5_000, datetime.now())
        assert redis.scores == {}

    def test_debit_clamps_at_zero(self, redis):
        dt = datetime.now()
        key = f"leaderboard:{dm._board_tokens(dt)[0][0]}:group:7"
        redis.scores[(key, "42")] = 1_000
        dm._debit_group_boards(42, 7, 5_000, dt)
        assert redis.scores[(key, "42")] == 0

    def test_debit_does_not_extend_retention(self, redis):
        dt = datetime.now()
        dm._credit_group_boards(42, 7, 5_000, dt)
        redis.expires.clear()
        dm._debit_group_boards(42, 7, 5_000, dt)
        assert redis.expires == []

    def test_zero_value_is_a_noop(self, redis):
        dm._debit_group_boards(42, 7, 0, datetime.now())
        assert redis.scores == {}

    def test_ancient_drop_touches_only_month_and_all_time(self, redis):
        old = datetime.now() - timedelta(days=500)
        tokens = [t for t, _ttl in dm._board_tokens(old)]
        # Day (90d) and week (~13mo) retention have both lapsed.
        assert tokens == [str(old.year * 100 + old.month), "all"]


# ── Notification retraction ───────────────────────────────────────────────────
class TestRetractGroupNotification:
    def test_only_the_matching_queued_row_is_dropped(self, models, outbox):
        mine = SimpleNamespace(data=json.dumps({"drop_id": 555}))
        theirs = SimpleNamespace(data=json.dumps({"drop_id": 556}))
        unparseable = SimpleNamespace(data="not json")
        session = FakeSession({FakeNotificationQueue: [mine, theirs, unparseable]})

        result = dm._retract_group_notification(session, 555, 7)

        assert session.deleted == [mine]
        assert result == {"dequeued": 1, "deleted": 0}

    def test_sent_notification_enqueues_a_delete(self, models, outbox):
        notified = SimpleNamespace(id=8, channel_id="123", message_id="456")
        session = FakeSession({FakeNotifiedSubmission: [notified]})

        result = dm._retract_group_notification(session, 555, 7)

        assert result == {"dequeued": 0, "deleted": 1}
        assert outbox == [{
            "channel_id": "123",
            "kind": "delete_message",
            "discord_message_id": "456",
            "ref_type": "notified_submission",
            "ref_id": 8,
            "commit": False,
        }]
        # Cleared inline, not after the bot confirms: the drop-notification
        # path skips any drop that still has a notified row, so waiting for the
        # next outbox drain would mute an undo-then-re-approve.
        assert session.deleted == [notified]

    def test_notified_row_without_a_message_id_is_just_dropped(self, models, outbox):
        # Nothing to delete in Discord, but the row still has to go or a
        # re-approval's notification gets deduped away.
        notified = SimpleNamespace(id=8, channel_id="123", message_id=None)
        session = FakeSession({FakeNotifiedSubmission: [notified]})

        result = dm._retract_group_notification(session, 555, 7)

        assert result == {"dequeued": 0, "deleted": 0}
        assert session.deleted == [notified]
        assert outbox == []
