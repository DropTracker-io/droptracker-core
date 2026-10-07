"""Clan bank (web131a): a group-level GP ledger.

One row per movement of the clan's GP: a member's **donation** in, a
**withdrawal** or **payout** out, a **transfer** into one of the group's event
prize pots, or a staff **adjustment** (an opening balance, a correction).
``amount`` is SIGNED (donations positive, outflows negative, adjustments
either), so the balance is simply ``SUM(amount) WHERE status='confirmed'``.

Like the event prize pot (:class:`db.models.events.EventBuyin`, which this is
modelled on) the ledger only records and advertises GP; the GP itself is traded
in game by the clan. Services live in ``services/group_bank.py``, routes in
``web_api/routes/group_bank.py``.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)

from .base import Base

# group_bank_entries.kind. Donations come in, withdrawals/payouts/transfers go
# out, adjustments carry their own sign.
BANK_ENTRY_KINDS = ("donation", "withdrawal", "payout", "event_transfer", "adjustment")
BANK_INFLOW_KINDS = ("donation",)
BANK_OUTFLOW_KINDS = ("withdrawal", "payout", "event_transfer")
# Only "confirmed" rows count toward the balance. "pending" waits in the staff
# review queue; "rejected" was turned down there; "void" = soft-removed after it
# had counted (kept for audit).
BANK_ENTRY_STATUSES = ("pending", "confirmed", "rejected", "void")
# Where the row came from: recorded by staff, self-reported by a member on the
# website, or self-reported through the Discord /bank command.
BANK_ENTRY_SOURCES = ("staff", "member", "discord")


class GroupBankEntry(Base):
    __tablename__ = "group_bank_entries"
    __table_args__ = (
        Index("idx_gbank_group_status", "group_id", "status"),
        Index("idx_gbank_group_created", "group_id", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    kind = Column(String(16), nullable=False)  # BANK_ENTRY_KINDS
    # Signed GP. BigInteger: a clan bank passes signed-INT 2.147B easily.
    amount = Column(BigInteger, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="confirmed")  # BANK_ENTRY_STATUSES
    source = Column(String(16), nullable=False, default="staff")  # BANK_ENTRY_SOURCES
    # The other side of the movement: who donated, or who was paid. NULL
    # player_id with a free-text rsn = someone with no tracked account.
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=True)
    rsn = Column(String(24), nullable=True)
    # The account that submitted the row (a member's self-report), if any.
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    note = Column(String(255), nullable=True)
    # Staff's reason when rejecting (shown to the submitter).
    review_note = Column(String(255), nullable=True)
    # Screenshot backing the row: a CDN URL built server-side from an uploaded
    # object key, never a client-supplied address (same rule as EventBuyin).
    proof_url = Column(String(255), nullable=True)
    # The event a transfer (or payout) belongs to. Deliberately not a
    # ForeignKey, like AuditLog.event_id: deleting an event must not be blocked
    # by, or cascade away, the bank's history.
    event_id = Column(Integer, nullable=True)
    # The prize-pot row a transfer created, so voiding one voids the other.
    event_buyin_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    # Staff member who last confirmed / rejected / edited the row.
    acted_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    confirmed_at = Column(DateTime, nullable=True)  # stamped when status -> confirmed
