"""Recap delivery ledger semantics (services/recap_delivery.py).

The ledger is what makes the 15-minute sweep idempotent, so its three rules
each guard a real failure mode:

* a `failed` row must NOT settle a subject — the model documents failures on
  our side as re-attemptable within the period, and the launch run proved why
  (a transport bug marked 147 subjects failed who were owed their card);
* a re-attempt must settle the SAME ledger slot rather than tripping the
  (scope, subject, period, kind, is_test) unique constraint;
* only a card that actually reached the recipient's side (sent, or bounced off
  their own closed DMs) may consume the one free unsolicited recap.

Loaded from the file path so the conftest ``services`` stub doesn't shadow it;
the ORM model and status constants are wired in as a real sqlite-backed model
because the conftest ``db`` stub would otherwise leave them as MagicMocks.
"""

import importlib.util
import os
import sys

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap_delivery.py",
)
_spec = importlib.util.spec_from_file_location("_recap_ledger_under_test", _MODULE_PATH)
delivery = importlib.util.module_from_spec(_spec)
sys.modules["_recap_ledger_under_test"] = delivery
_spec.loader.exec_module(delivery)

_Base = declarative_base()


class RecapDelivery(_Base):
    """Shape-compatible stand-in for db.models.recap.RecapDelivery."""

    __tablename__ = "recap_deliveries"

    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)
    subject_id = Column(Integer, nullable=False)
    period = Column(String(7), nullable=False)
    kind = Column(String(16), nullable=False)
    user_id = Column(Integer)
    target_id = Column(String(32))
    status = Column(String(16), nullable=False)
    message_id = Column(String(32))
    error = Column(String(500))
    is_test = Column(Integer, nullable=False, default=0)


_CONSTANTS = {
    "DELIVERY_DM": "dm",
    "DELIVERY_CHANNEL": "channel",
    "DELIVERY_SENT": "sent",
    "DELIVERY_FORBIDDEN": "forbidden",
    "DELIVERY_FAILED": "failed",
    "DELIVERY_NO_CARD": "no_card",
}
delivery.RecapDelivery = RecapDelivery
for _name, _value in _CONSTANTS.items():
    setattr(delivery, _name, _value)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _row(session, status, *, kind="dm", subject_id=7, user_id=42, is_test=0):
    row = RecapDelivery(
        scope="player", subject_id=subject_id, period="2026-07", kind=kind,
        user_id=user_id, status=status, is_test=is_test,
    )
    session.add(row)
    session.commit()
    return row


class TestAlreadyDelivered:
    def test_sent_settles_the_subject(self, session):
        _row(session, "sent")
        assert delivery.already_delivered(session, "player", 7, "2026-07", "dm", False)

    def test_forbidden_settles_the_subject(self, session):
        _row(session, "forbidden")
        assert delivery.already_delivered(session, "player", 7, "2026-07", "dm", False)

    def test_no_card_settles_the_subject(self, session):
        _row(session, "no_card")
        assert delivery.already_delivered(session, "player", 7, "2026-07", "dm", False)

    def test_failed_leaves_the_subject_due(self, session):
        _row(session, "failed")
        assert not delivery.already_delivered(session, "player", 7, "2026-07", "dm", False)

    def test_test_rows_do_not_settle_real_delivery(self, session):
        _row(session, "sent", is_test=1)
        assert not delivery.already_delivered(session, "player", 7, "2026-07", "dm", False)


class TestRecordDelivery:
    def test_first_record_inserts(self, session):
        delivery.record_delivery(
            session, scope="player", subject_id=7, period="2026-07",
            kind="dm", status="failed", user_id=42, error="boom",
        )
        session.commit()
        rows = session.query(RecapDelivery).all()
        assert len(rows) == 1 and rows[0].status == "failed"

    def test_reattempt_updates_the_same_slot(self, session):
        _row(session, "failed")
        delivery.record_delivery(
            session, scope="player", subject_id=7, period="2026-07",
            kind="dm", status="sent", user_id=42, message_id="123",
        )
        session.commit()
        rows = session.query(RecapDelivery).all()
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].message_id == "123"
        assert rows[0].error is None

    def test_test_and_real_rows_are_separate_slots(self, session):
        _row(session, "sent", is_test=1)
        delivery.record_delivery(
            session, scope="player", subject_id=7, period="2026-07",
            kind="dm", status="sent", user_id=42,
        )
        session.commit()
        assert session.query(RecapDelivery).count() == 2


class TestFreeRecapEntitlement:
    def test_sent_consumes_the_free_recap(self, session):
        _row(session, "sent")
        assert delivery.user_had_prior_recap(session, 42)

    def test_closed_dms_consume_it_too(self, session):
        _row(session, "forbidden")
        assert delivery.user_had_prior_recap(session, 42)

    def test_a_failure_on_our_side_does_not(self, session):
        _row(session, "failed")
        assert not delivery.user_had_prior_recap(session, 42)

    def test_a_month_with_no_card_does_not(self, session):
        _row(session, "no_card")
        assert not delivery.user_had_prior_recap(session, 42)

    def test_test_deliveries_do_not(self, session):
        _row(session, "sent", is_test=1)
        assert not delivery.user_had_prior_recap(session, 42)
