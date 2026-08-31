"""A dead transaction must stay retryable, end to end.

2026-08-30, 18:56-00:41 UTC: MariaDB timed out mid-query during evening peak
(``OperationalError`` 2013). The timeouts landed inside the KC-milestone check,
whose ``except Exception`` exists so that "a failure here must never cost the
drop itself" — but it swallowed the error without rolling back, leaving the
session needing one. The processor's own ``session.commit()`` on the next line
then raised ``PendingRollbackError``, which subclasses ``InvalidRequestError``
rather than ``DBAPIError`` and so read as *poison* to
``_is_retryable``. Result: 27 envelopes / 42 submissions across 27 players
dead-lettered on their FIRST failure with zero retries, and the guard that
promised never to cost the drop cost every one of them.

Two independent regressions are fenced here, because either alone would have
prevented the incident:

1. the swallow no longer hides an infrastructure fault (the source), and
2. ``PendingRollbackError`` is retryable (the backstop, for the next swallow).
"""

import ast
import inspect

from sqlalchemy.exc import (
    DBAPIError, IntegrityError, InterfaceError, InvalidRequestError,
    OperationalError, PendingRollbackError,
)

import workers.webhook_consumer as wc
from data.submissions.common import reraise_if_session_broken


def _op_error(msg="Lost connection to MySQL server during query (timed out)"):
    return OperationalError("SELECT 1", {}, Exception(msg))


class TestPoisonedSessionIsRetryable:
    """The consumer must not read a poisoned session as a bad payload."""

    def test_pending_rollback_is_retryable(self):
        # The exact error that dead-lettered 27 envelopes.
        assert wc._is_retryable(PendingRollbackError("Can't reconnect until "
                                                     "invalid transaction is "
                                                     "rolled back")) is True

    def test_pending_rollback_is_not_a_dbapi_error(self):
        """Why the isinstance chain missed it — the reason this test exists.

        If SQLAlchemy ever reparents it under DBAPIError this assertion fails
        loudly, which is the moment to re-check whether the explicit branch in
        _is_retryable is still doing anything.
        """
        assert issubclass(PendingRollbackError, InvalidRequestError)
        assert not issubclass(PendingRollbackError, DBAPIError)

    def test_underlying_db_faults_stay_retryable(self):
        assert wc._is_retryable(_op_error()) is True
        assert wc._is_retryable(InterfaceError("x", {}, Exception())) is True

    def test_genuine_poison_is_still_not_retried(self):
        """The fix must not turn the dead-letter queue into an infinite loop."""
        assert wc._is_retryable(ValueError("unparseable payload")) is False
        assert wc._is_retryable(KeyError("player_name")) is False
        assert wc._is_retryable(TypeError()) is False


class TestSwallowedSideEffectsCannotPoisonTheCommit:
    """`reraise_if_session_broken` is the source-side half of the fix."""

    def test_infrastructure_faults_are_reraised(self):
        for exc in (_op_error(),
                    InterfaceError("x", {}, Exception()),
                    PendingRollbackError("x")):
            try:
                reraise_if_session_broken(exc)
            except Exception as raised:
                assert raised is exc
            else:
                raise AssertionError(
                    f"{type(exc).__name__} was swallowed — it would poison the "
                    "commit and dead-letter the envelope")

    def test_invalidated_connection_is_reraised(self):
        exc = DBAPIError("x", {}, Exception())
        exc.connection_invalidated = True
        try:
            reraise_if_session_broken(exc)
        except DBAPIError:
            pass
        else:
            raise AssertionError("invalidated connection was swallowed")

    def test_milestone_logic_faults_are_still_swallowed(self):
        """The guard's real purpose survives: these must never cost the drop.

        IntegrityError is the interesting one — a DBAPIError subclass whose
        connection is fine, so it must NOT be treated as a broken session.
        """
        integrity = IntegrityError("INSERT", {}, Exception("duplicate"))
        assert getattr(integrity, "connection_invalidated", False) is False
        for exc in (ValueError("unknown boss"), KeyError("npc_id"),
                    AttributeError(), integrity):
            reraise_if_session_broken(exc)  # must not raise


class TestKcMilestoneHandlersCallTheGuard:
    """AST-scan both processors, the way TestGuidDedupIsTransportBlind does.

    A future edit that re-broadens these handlers back to a bare swallow
    reintroduces the incident silently — nothing else in the suite would fail.
    """

    def _kc_handlers(self, module):
        tree = ast.parse(inspect.getsource(module))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            # The KC block is the try that imports handle_kill_count directly
            # in its own body. Matching anywhere inside would also select every
            # enclosing try in the processor, whose handlers are unrelated.
            if any(isinstance(stmt, ast.ImportFrom)
                   and any(a.name == "handle_kill_count" for a in stmt.names)
                   for stmt in node.body):
                found.extend(node.handlers)
        return found

    def _asserts_guarded(self, module):
        handlers = self._kc_handlers(module)
        assert handlers, f"no KC-milestone try/except found in {module.__name__}"
        for handler in handlers:
            calls = [n.func.id for n in ast.walk(handler)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
            assert "reraise_if_session_broken" in calls, (
                f"{module.__name__}: the KC-milestone handler swallows every "
                "exception again. An infrastructure fault there poisons the "
                "session, so the processor's commit raises PendingRollbackError "
                "and the envelope is dead-lettered with no retries.")

    def test_drop_processor_guards_its_kc_handler(self):
        from data.submissions import drop
        self._asserts_guarded(drop)

    def test_pb_processor_guards_its_kc_handler(self):
        from data.submissions import pb
        self._asserts_guarded(pb)
