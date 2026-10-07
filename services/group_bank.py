"""Clan bank rules (web131a): the pure half of the group GP ledger.

The ledger itself is :class:`db.models.group_bank.GroupBankEntry`; the routes
are ``web_api/routes/group_bank.py``. This module holds every rule those routes
apply that doesn't need a database: how a kind maps to a sign, what an amount
may be, which status moves are allowed, and how a set of rows rolls up into the
headline numbers.

Stdlib-only at import time, like ``services/event_prizes.py``: the core bot
(the Phase 2 ``/bank`` command) imports it, and the unit-test conftest stubs
the ``services`` package, so tests load this file by path.
"""
from __future__ import annotations

from typing import Iterable, Optional

# Re-declared locally (not imported from db.models) so this module stays
# stdlib-only. Keep in sync with db/models/group_bank.py.
BANK_ENTRY_KINDS = ("donation", "withdrawal", "payout", "event_transfer", "adjustment")
INFLOW_KINDS = ("donation",)
OUTFLOW_KINDS = ("withdrawal", "payout", "event_transfer")
BANK_ENTRY_STATUSES = ("pending", "confirmed", "rejected", "void")

# Kinds a person can record directly. Transfers into an event pot go through
# their own route, which writes the matching prize-pot row in the same commit.
RECORDABLE_KINDS = ("donation", "withdrawal", "payout", "adjustment")

# Kinds that must name the other side: who gave the GP, or who received it.
NAMED_KINDS = ("donation", "payout")

# Same ceiling as the prize pot (services/event_prizes.MAX_BUYIN_AMOUNT): far
# above any real clan bank, far below signed BIGINT, so a fat-fingered value
# can't poison the ledger.
MAX_BANK_AMOUNT = 10 ** 15

# Status moves a staff edit may make. "void" is reached only through DELETE
# (which also voids a transfer's prize-pot row), and a void row stays void.
_TRANSITIONS = {
    "pending": ("confirmed", "rejected"),
    "confirmed": ("pending",),
    "rejected": ("pending",),
    "void": (),
}

TOP_DONORS_LIMIT = 10


class BankRuleError(ValueError):
    """A rule violation the route turns into a 4xx problem response."""

    def __init__(self, title: str, detail: str, status: int = 422):
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.status = status


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def signed_amount(kind: str, value) -> int:
    """The signed GP stored for ``kind``, from what a person typed.

    Donations and outflows are entered as a positive number of GP and stored
    with the kind's sign. An adjustment carries its own sign (an opening
    balance is positive, a correction may be negative) and must not be 0.
    """
    if kind not in BANK_ENTRY_KINDS:
        raise BankRuleError("Invalid kind", f"'kind' must be one of {list(BANK_ENTRY_KINDS)}.")
    if not _is_int(value):
        raise BankRuleError("Invalid amount", "'amount' must be a whole number of GP.")
    if kind == "adjustment":
        if value == 0:
            raise BankRuleError("Invalid amount", "An adjustment can't be 0 GP.")
        if abs(value) >= MAX_BANK_AMOUNT:
            raise BankRuleError("Invalid amount", f"'amount' must be below {MAX_BANK_AMOUNT:,} GP.")
        return value
    if not (0 < value < MAX_BANK_AMOUNT):
        raise BankRuleError(
            "Invalid amount", f"'amount' must be between 1 and {MAX_BANK_AMOUNT - 1:,} GP."
        )
    return -value if kind in OUTFLOW_KINDS else value


def check_transition(old: str, new: str) -> None:
    """Raise unless a staff edit may move a row from ``old`` to ``new``."""
    if new == old:
        return
    if new not in BANK_ENTRY_STATUSES:
        raise BankRuleError("Invalid status", f"'status' must be one of {list(BANK_ENTRY_STATUSES)}.")
    if new == "void":
        raise BankRuleError("Use delete", "Remove the entry to void it.")
    if new not in _TRANSITIONS.get(old, ()):
        raise BankRuleError(
            "Status change not allowed",
            f"An entry that is {old} can't be marked {new}.",
            status=409,
        )


def clean_text(value, field: str, limit: int = 255) -> Optional[str]:
    """Strip a free-text field; empty becomes None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BankRuleError(f"Invalid {field}", f"'{field}' must be a string.")
    value = value.strip()
    if len(value) > limit:
        raise BankRuleError(f"Invalid {field}", f"'{field}' must be at most {limit} characters.")
    return value or None


def totals_from_kind_sums(kind_sums: Iterable) -> dict:
    """Headline numbers from ``(kind, SUM(amount), COUNT(*))`` over confirmed rows.

    ``paid_out`` is reported as a positive number; ``adjustments`` keeps its
    sign. ``balance`` is everything added together.
    """
    donated = paid_out = adjustments = 0
    donation_count = 0
    by_kind = {}
    for kind, total, count in kind_sums:
        total = int(total or 0)
        by_kind[kind] = by_kind.get(kind, 0) + total
        if kind in INFLOW_KINDS:
            donated += total
            donation_count += int(count or 0)
        elif kind in OUTFLOW_KINDS:
            paid_out += -total
        else:
            adjustments += total
    return {
        "balance": donated - paid_out + adjustments,
        "donated": donated,
        "paid_out": paid_out,
        "adjustments": adjustments,
        "donation_count": donation_count,
        "by_kind": by_kind,
    }


def merge_donors(
    by_player: Iterable,
    by_name: Iterable,
    player_names: dict,
    limit: int = TOP_DONORS_LIMIT,
) -> tuple:
    """Rank donors from two aggregates over confirmed donations.

    ``by_player`` is ``(player_id, SUM, COUNT)`` for tracked accounts (shown
    under their current name from ``player_names``); ``by_name`` is
    ``(rsn, SUM, COUNT)`` for free-text donors with no account, merged
    case-insensitively. Returns ``(top_donors, donor_count)``.
    """
    donors = []
    for pid, total, count in by_player:
        donors.append({
            "player_id": int(pid),
            "rsn": player_names.get(pid),
            "total": int(total or 0),
            "count": int(count or 0),
        })
    named: dict = {}
    for rsn, total, count in by_name:
        label = (rsn or "").strip()
        if not label:
            continue
        key = label.lower()
        entry = named.setdefault(key, {"player_id": None, "rsn": label, "total": 0, "count": 0})
        entry["total"] += int(total or 0)
        entry["count"] += int(count or 0)
    donors.extend(named.values())
    donors.sort(key=lambda d: (-d["total"], (d["rsn"] or "").lower()))
    return donors[:limit], len(donors)
