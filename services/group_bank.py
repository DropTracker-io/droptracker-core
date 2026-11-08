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

import hashlib
from datetime import datetime, timedelta
from typing import Iterable, Optional

# Re-declared locally (not imported from db.models) so this module stays
# stdlib-only. Keep in sync with db/models/group_bank.py.
BANK_ENTRY_KINDS = (
    "donation", "withdrawal", "payout", "event_transfer", "adjustment", "holder_transfer",
)
INFLOW_KINDS = ("donation",)
OUTFLOW_KINDS = ("withdrawal", "payout", "event_transfer")
BANK_ENTRY_STATUSES = ("pending", "confirmed", "rejected", "void")

# Kinds a person can record directly. Transfers into an event pot go through
# their own route, which writes the matching prize-pot row in the same commit.
RECORDABLE_KINDS = ("donation", "withdrawal", "payout", "adjustment", "holder_transfer")

# Kinds that must name the other side: who gave the GP, or who received it.
# A holder transfer names who received it; the holder is who passed it on.
NAMED_KINDS = ("donation", "payout", "holder_transfer")

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

# An event run outside DropTracker is named by the clan; the name is stored on
# the transfer (group_bank_entries.event_label, VARCHAR(64)).
OUTSIDE_EVENT_NAME_LIMIT = 64

# A backdated entry can't predate Old School itself (launched 2013-02-22), and
# can't be in the future beyond a little clock slack between browser and server.
EARLIEST_ENTRY_DATE = datetime(2013, 2, 22)
ENTRY_DATE_SLACK = timedelta(minutes=10)

# --- In-game coffer sync (web144a) ------------------------------------------
# Relayed "X has deposited N coins into the coffer." lines become bank rows
# (data/submissions/clan_broadcast.py). Parsed kind -> bank kind.
COFFER_KINDS = {"coffer_donation": "donation", "coffer_withdrawal": "withdrawal"}

# bank_coffer_status values. Anything else reads as "pending": a relayed line
# is only as trustworthy as the clanmate who sent it, so the safe reading wins.
COFFER_STATUSES = ("pending", "confirmed")

# Dedupe window. Every relayer of one line is accepted by the server within
# seconds of the others, so a line is matched against its own bucket and the
# one before (a relay straddling the boundary still collapses). 60s keeps the
# window short enough that two real, identical deposits a few minutes apart
# are never merged.
COFFER_BUCKET_SECONDS = 60

# How long one relayer's count of an identical line lives. Must outlast the
# two-bucket match window, so a real repeat inside it is still told apart.
COFFER_OCCURRENCE_TTL_SECONDS = 180


def coffer_status(raw) -> str:
    """The status new coffer rows get from the ``bank_coffer_status`` config."""
    value = str(raw or "").strip().lower()
    return value if value in COFFER_STATUSES else "pending"


def coffer_bucket(timestamp: float) -> int:
    return int(timestamp // COFFER_BUCKET_SECONDS)


def coffer_ref(clan_slug: str, message: str, bucket: int, occurrence: int) -> str:
    """Deterministic ``group_bank_entries.external_ref`` for one coffer line.

    ``occurrence`` is how many times THIS relayer has sent the identical line
    lately (1 for the first). N relayers seeing one deposit all send
    occurrence 1 and collapse onto one row; one relayer seeing the same
    deposit twice sends 1 then 2, which is how two real, identical deposits
    in a row both book. 71 chars, inside the String(80) column."""
    digest = hashlib.sha256(
        f"{clan_slug}|{message}|{bucket}|{occurrence}".encode("utf-8")
    ).hexdigest()
    return f"coffer:{digest}"


def coffer_refs(clan_slug: str, message: str, timestamp: float, occurrence: int) -> tuple:
    """``(ref to write, refs that mean "already booked")`` for a line accepted
    at ``timestamp``: this bucket's ref, plus the previous bucket's."""
    bucket = coffer_bucket(timestamp)
    current = coffer_ref(clan_slug, message, bucket, occurrence)
    return current, (current, coffer_ref(clan_slug, message, bucket - 1, occurrence))


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
    balance is positive, a correction may be negative) and must not be 0. A
    holder transfer is stored positive: it never touches the balance.
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


def transfer_target(event_id, event_name) -> tuple:
    """``(event_id, None)`` for one of the group's DropTracker events, or
    ``(None, name)`` for an event the clan runs elsewhere. Exactly one."""
    if event_id is not None and not _is_int(event_id):
        raise BankRuleError("Invalid event_id", "'event_id' must be an integer.")
    name = clean_text(event_name, "event_name", OUTSIDE_EVENT_NAME_LIMIT)
    if event_id is not None and name:
        raise BankRuleError(
            "Choose one event", "Send an 'event_id' or an 'event_name', not both."
        )
    if event_id is None and not name:
        raise BankRuleError(
            "Event required",
            "Send the 'event_id' of one of your events, or an 'event_name' for one run outside DropTracker.",
        )
    return event_id, name


def entry_date(value, now: datetime) -> datetime:
    """When an entry happened, from a unix timestamp a person picked (to
    backdate a record). Returned naive UTC, like every ledger column."""
    if not _is_int(value):
        raise BankRuleError("Invalid date", "'created_at' must be a unix timestamp in seconds.")
    try:
        when = datetime.utcfromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        raise BankRuleError("Invalid date", "'created_at' is out of range.")
    if when < EARLIEST_ENTRY_DATE:
        raise BankRuleError("Invalid date", "The date can't be before Old School launched (2013-02-22).")
    if when > now + ENTRY_DATE_SLACK:
        raise BankRuleError("Invalid date", "The date can't be in the future.")
    return when


def is_coffer_move(kind: str, source: Optional[str], coffer_withdrawals_neutral: bool) -> bool:
    """True for a synced coffer withdrawal the group counts as GP moving out
    of the coffer but staying the clan's (``bank_coffer_withdrawals_neutral``).
    The coffer holds at most 50M, so clans take GP out to hold it; that isn't
    spending. Only synced lines: a withdrawal staff record is GP leaving."""
    return bool(coffer_withdrawals_neutral) and kind == "withdrawal" and source == "coffer"


def totals_from_kind_sums(kind_sums: Iterable) -> dict:
    """Headline numbers from ``(kind, SUM(amount), COUNT(*))`` over confirmed rows.

    ``paid_out`` is every outflow, reported as a positive number, and
    ``withdrawn`` / ``payouts`` / ``transferred`` split it by kind (a coffer
    withdrawal is not a prize payout, and the page says which is which).
    ``adjustments`` keeps its sign. ``balance`` is everything added together.
    Holder transfers never count (see :func:`roll_up`, which leaves every
    held move out before this runs).
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
        elif kind == "adjustment":
            adjustments += total
    return {
        "balance": donated - paid_out + adjustments,
        "donated": donated,
        "paid_out": paid_out,
        "withdrawn": -by_kind.get("withdrawal", 0),
        "payouts": -by_kind.get("payout", 0),
        "transferred": -by_kind.get("event_transfer", 0),
        "adjustments": adjustments,
        "donation_count": donation_count,
        "by_kind": by_kind,
    }


# --- Holders (suggestion #160, web159a) --------------------------------------
# The coffer holds at most 50M, so clans keep GP with their staff. A row's
# holder (holder_player_id / holder_rsn) is the person whose keeping it moved
# GP into or out of:
#   withdrawal       -> the GP went to the holder; the clan still has it
#   donation         -> held GP went back into the coffer (a return)
#   holder_transfer  -> passed from the holder to the named person
#   payout / event_transfer / adjustment -> paid from (or, for a positive
#                       adjustment, recorded as held by) the holder
# The first three move GP within the clan, so they stay out of the balance.
# A synced coffer withdrawal with no holder set is held by whoever withdrew
# it while bank_coffer_withdrawals_neutral is on (see is_coffer_move).

def is_held_move(kind: str, source: Optional[str], has_holder: bool,
                 coffer_withdrawals_neutral: bool) -> bool:
    """True for a row that moves GP between the coffer and the staff who hold
    it, or between two of them: listed in the ledger, kept out of the
    balance and the totals."""
    if kind == "holder_transfer":
        return True
    if has_holder and kind in ("withdrawal", "donation"):
        return True
    return is_coffer_move(kind, source, coffer_withdrawals_neutral)


def holder_key(player_id, rsn) -> Optional[tuple]:
    """Who a holder is: a tracked account by id, a free-text name under OSRS
    name equivalence (case, spaces, hyphens and underscores alike)."""
    if player_id is not None:
        return ("p", int(player_id))
    label = " ".join(str(rsn or "").replace("-", " ").replace("_", " ").split()).lower()
    return ("n", label) if label else None


def holder_moves(kind: str, source: Optional[str], amount: int, player: tuple, holder: tuple,
                 coffer_withdrawals_neutral: bool) -> list:
    """``[((player_id, rsn), delta), ...]``: how one confirmed row changes
    what each person holds. ``player`` is the row's other side and
    ``holder`` its holder, both ``(player_id, rsn)``; ``amount`` is the
    stored, signed GP."""
    amount = int(amount or 0)
    has_holder = holder_key(*holder) is not None
    if kind == "holder_transfer":
        return [(holder, -amount), (player, amount)] if has_holder else []
    if kind in ("withdrawal", "donation"):
        if has_holder:
            # Stored negative for a withdrawal (into their keeping), positive
            # for a return (out of it).
            return [(holder, -amount)]
        if is_coffer_move(kind, source, coffer_withdrawals_neutral) and holder_key(*player):
            return [(player, -amount)]
        return []
    if has_holder:
        # Outflows are stored negative: paying out lowers what they hold.
        return [(holder, amount)]
    return []


def check_holder(kind: str, player: tuple, holder: tuple) -> None:
    """Raise unless a row's holder fits its kind: a holder transfer needs one,
    and it can't hand the GP to the same person."""
    if kind != "holder_transfer":
        return
    hk = holder_key(*holder)
    if hk is None:
        raise BankRuleError(
            "Holder required", "Name who passed the GP on ('holder_player_id' or 'holder_rsn').",
        )
    if hk == holder_key(*player):
        raise BankRuleError("Same person", "The GP has to go to someone else.")


def roll_up(rows: Iterable, coffer_withdrawals_neutral: bool, player_names: dict) -> dict:
    """Everything the headline needs, from confirmed rows grouped by
    ``(kind, source, player_id, rsn, holder_player_id, holder_rsn, SUM(amount),
    COUNT(*))``.

    Returns ``kind_sums`` (for :func:`totals_from_kind_sums`; held moves
    left out), ``donors`` (``by_player`` / ``by_name`` for
    :func:`merge_donors`; returns are not donations), ``holders`` (one per
    person, most held first; someone who held GP and paid it all out stays
    listed at 0) and ``held`` (their sum: the clan's GP outside the coffer).
    """
    by_kind: dict = {}
    by_player: dict = {}
    by_name: dict = {}
    holders: dict = {}
    for kind, source, pid, rsn, hpid, hrsn, total, count in rows:
        total = int(total or 0)
        count = int(count or 0)
        has_holder = holder_key(hpid, hrsn) is not None
        for (who_pid, who_rsn), delta in holder_moves(
            kind, source, total, (pid, rsn), (hpid, hrsn), coffer_withdrawals_neutral
        ):
            key = holder_key(who_pid, who_rsn)
            if key is None:
                continue
            entry = holders.setdefault(key, {
                "player_id": who_pid,
                "rsn": player_names.get(who_pid) if who_pid is not None else None,
                "held": 0,
            })
            if entry["rsn"] is None:
                entry["rsn"] = (str(who_rsn).strip() or None) if who_rsn else None
            entry["held"] += delta
        if is_held_move(kind, source, has_holder, coffer_withdrawals_neutral):
            continue
        t, c = by_kind.get(kind, (0, 0))
        by_kind[kind] = (t + total, c + count)
        if kind == "donation":
            if pid is not None:
                t, c = by_player.get(pid, (0, 0))
                by_player[pid] = (t + total, c + count)
            else:
                t, c = by_name.get(rsn, (0, 0))
                by_name[rsn] = (t + total, c + count)
    listed = sorted(holders.values(), key=lambda h: (-h["held"], (h["rsn"] or "").lower()))
    return {
        "kind_sums": [(k, t, c) for k, (t, c) in by_kind.items()],
        "by_player": [(p, t, c) for p, (t, c) in by_player.items()],
        "by_name": [(n, t, c) for n, (t, c) in by_name.items()],
        "holders": listed,
        "held": sum(h["held"] for h in listed),
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
