"""Unit tests for the clan bank (web131a):

- the pure rules in ``services/group_bank.py`` (signs, amount limits, status
  moves, roll-ups), loaded for real by the conftest; and
- the bank routes in ``web_api/routes/group_bank.py``, driven through the app
  with a scripted session, plus the prize-pot side of a transfer
  (``web_api/routes/event_prizes.py``).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.group_bank as gbs
import web_api.routes.event_prizes as epr
import web_api.routes.group_bank as gb
from web_api.common import ProblemException
from tests.unit.test_event_auth_modes import _Q, _SessionCM, _event


# --------------------------------------------------------------------------- #
# Pure rules
# --------------------------------------------------------------------------- #
class TestSignedAmount:
    def test_donation_positive_outflows_negative(self):
        assert gbs.signed_amount("donation", 1_000) == 1_000
        assert gbs.signed_amount("withdrawal", 1_000) == -1_000
        assert gbs.signed_amount("payout", 1_000) == -1_000
        assert gbs.signed_amount("event_transfer", 1_000) == -1_000

    def test_adjustment_keeps_its_sign(self):
        assert gbs.signed_amount("adjustment", 5_000) == 5_000
        assert gbs.signed_amount("adjustment", -5_000) == -5_000

    @pytest.mark.parametrize("kind,value", [
        ("donation", 0),
        ("donation", -5),
        ("payout", gbs.MAX_BANK_AMOUNT),
        ("adjustment", 0),
        ("adjustment", -gbs.MAX_BANK_AMOUNT),
        ("donation", 1.5),
        ("donation", True),
        ("donation", "100"),
        ("donation", None),
    ])
    def test_rejects_bad_amounts(self, kind, value):
        with pytest.raises(gbs.BankRuleError):
            gbs.signed_amount(kind, value)

    def test_rejects_unknown_kind(self):
        with pytest.raises(gbs.BankRuleError):
            gbs.signed_amount("heist", 10)


class TestCheckTransition:
    @pytest.mark.parametrize("old,new", [
        ("pending", "confirmed"),
        ("pending", "rejected"),
        ("confirmed", "pending"),
        ("rejected", "pending"),
        ("confirmed", "confirmed"),
    ])
    def test_allowed(self, old, new):
        gbs.check_transition(old, new)

    @pytest.mark.parametrize("old,new", [
        ("rejected", "confirmed"),   # reopen first, then confirm
        ("confirmed", "rejected"),
        ("void", "confirmed"),
        ("pending", "void"),         # voiding is DELETE's job
        ("pending", "banana"),
    ])
    def test_refused(self, old, new):
        with pytest.raises(gbs.BankRuleError):
            gbs.check_transition(old, new)


class TestCleanText:
    def test_strips_and_blanks(self):
        assert gbs.clean_text("  hi  ", "note") == "hi"
        assert gbs.clean_text("   ", "note") is None
        assert gbs.clean_text(None, "note") is None

    def test_limits(self):
        with pytest.raises(gbs.BankRuleError):
            gbs.clean_text("x" * 25, "rsn", 24)
        with pytest.raises(gbs.BankRuleError):
            gbs.clean_text(5, "note")


class TestTransferTarget:
    def test_one_of_ours_or_a_named_outside_event(self):
        assert gbs.transfer_target(5, None) == (5, None)
        assert gbs.transfer_target(None, "  Weekly PvM ") == (None, "Weekly PvM")
        # A blank name with an id is just the id.
        assert gbs.transfer_target(5, "  ") == (5, None)

    @pytest.mark.parametrize("event_id,name", [
        (None, None), (None, "   "), (5, "Weekly PvM"), ("5", None), (True, None),
        (None, "x" * 65), (None, 7),
    ])
    def test_refused(self, event_id, name):
        with pytest.raises(gbs.BankRuleError):
            gbs.transfer_target(event_id, name)


class TestTotals:
    def test_balance_from_kind_sums(self):
        t = gbs.totals_from_kind_sums([
            ("donation", 10_000, 3),
            ("withdrawal", -2_000, 1),
            ("payout", -1_000, 1),
            ("event_transfer", -500, 1),
            ("adjustment", 250, 1),
        ])
        assert t["donated"] == 10_000
        assert t["paid_out"] == 3_500          # reported positive
        assert t["adjustments"] == 250
        assert t["balance"] == 10_000 - 3_500 + 250
        assert t["donation_count"] == 3

    def test_empty(self):
        t = gbs.totals_from_kind_sums([])
        assert t["balance"] == 0 and t["by_kind"] == {}

    def test_outflows_split_by_kind(self):
        # A coffer withdrawal is not a prize payout; the page shows each.
        t = gbs.totals_from_kind_sums([
            ("withdrawal", -2_000, 1), ("payout", -1_000, 1), ("event_transfer", -500, 1),
        ])
        assert (t["withdrawn"], t["payouts"], t["transferred"]) == (2_000, 1_000, 500)
        assert t["paid_out"] == 3_500
        assert gbs.totals_from_kind_sums([])["withdrawn"] == 0

    def test_can_go_negative(self):
        # A payout recorded before the opening balance is allowed; the page
        # shows the negative balance rather than the ledger refusing it.
        assert gbs.totals_from_kind_sums([("payout", -100, 1)])["balance"] == -100


class TestMergeDonors:
    def test_players_and_free_text_ranked_together(self):
        top, count = gbs.merge_donors(
            [(5, 3_000, 2), (6, 500, 1)],
            [("Sponsor", 1_000, 1), ("sponsor", 1_500, 1), ("", 99, 1)],
            {5: "Zez", 6: "Bob"},
        )
        assert [d["rsn"] for d in top] == ["Zez", "Sponsor", "Bob"]
        # Free-text names merge case-insensitively; blank names are dropped.
        assert top[1]["total"] == 2_500 and top[1]["count"] == 2
        assert top[1]["player_id"] is None
        assert count == 3

    def test_limit(self):
        rows = [(i, 100 + i, 1) for i in range(20)]
        top, count = gbs.merge_donors(rows, [], {}, limit=5)
        assert len(top) == 5 and count == 20
        assert top[0]["player_id"] == 19


def _g(kind, source, total, count=1, pid=None, rsn=None, hpid=None, hrsn=None):
    """One grouped row as ``_summary`` selects it for ``roll_up``."""
    return (kind, source, pid, rsn, hpid, hrsn, total, count)


class TestCofferMoves:
    ROWS = [
        _g("donation", "coffer", 45_000_000, 9, rsn="Zez"),
        _g("donation", "staff", 1_000, 1, rsn="Zez"),
        _g("withdrawal", "coffer", -36_069_421, 2, rsn="xCmase"),
        _g("withdrawal", "staff", -500, 1),
        _g("payout", "staff", -2_000, 1, rsn="Winner"),
    ]

    def test_off_everything_counts(self):
        out = gbs.roll_up(self.ROWS, False, {})
        t = gbs.totals_from_kind_sums(out["kind_sums"])
        assert out["held"] == 0 and out["holders"] == []
        assert t["withdrawn"] == 36_069_921
        assert t["balance"] == 45_001_000 - 36_069_921 - 2_000

    def test_on_coffer_withdrawals_leave_the_totals(self):
        out = gbs.roll_up(self.ROWS, True, {})
        t = gbs.totals_from_kind_sums(out["kind_sums"])
        # Held by whoever took it out of the coffer.
        assert out["held"] == 36_069_421
        assert out["holders"] == [{"player_id": None, "rsn": "xCmase", "held": 36_069_421}]
        # A withdrawal staff record is still GP leaving the clan.
        assert t["withdrawn"] == 500 and t["payouts"] == 2_000
        assert t["paid_out"] == 2_500
        assert t["balance"] == 45_001_000 - 2_500
        # Deposits from either source still merge into one donation count.
        assert t["donated"] == 45_001_000 and t["donation_count"] == 10

    def test_only_synced_withdrawals_are_moves(self):
        assert gbs.is_coffer_move("withdrawal", "coffer", True)
        assert not gbs.is_coffer_move("withdrawal", "coffer", False)
        assert not gbs.is_coffer_move("withdrawal", "staff", True)
        assert not gbs.is_coffer_move("donation", "coffer", True)
        assert not gbs.is_coffer_move("payout", "coffer", True)


class TestHolders:
    """Suggestion #160 (2026-11-07): who holds the clan's GP outside the coffer."""

    M = 1_000_000
    # The thread's flow, coffer withdrawals neutral: a donation, an admin
    # takes it out to hold it, pays a prize from it, hands some to another
    # admin, who returns part of it, and an outside event is funded.
    ROWS = [
        _g("donation", "coffer", 50 * M, rsn="Zez"),
        _g("withdrawal", "coffer", -50 * M, rsn="Mahj"),
        _g("payout", "staff", -20 * M, rsn="Winner", hrsn="Mahj"),
        _g("holder_transfer", "staff", 10 * M, rsn="Croc", hrsn="Mahj"),
        _g("donation", "coffer", 5 * M, rsn="Croc", hrsn="Croc"),
        _g("event_transfer", "staff", -3 * M, hrsn="mahj"),
    ]

    def test_flow_from_the_thread(self):
        out = gbs.roll_up(self.ROWS, True, {})
        t = gbs.totals_from_kind_sums(out["kind_sums"])
        held = {h["rsn"]: h["held"] for h in out["holders"]}
        # "mahj" is the same person as "Mahj" (OSRS names ignore case).
        assert held == {"Mahj": 17 * self.M, "Croc": 5 * self.M}
        assert out["held"] == 22 * self.M
        # The return and the hand-over never touch the balance: what's left
        # is 5M in the coffer plus 22M with staff.
        assert t["balance"] == 27 * self.M
        assert t["donated"] == 50 * self.M and t["donation_count"] == 1
        assert t["payouts"] == 20 * self.M and t["transferred"] == 3 * self.M
        assert t["withdrawn"] == 0
        # A return isn't a donation.
        assert out["by_name"] == [("Zez", 50 * self.M, 1)]

    def test_named_holder_keeps_a_staff_withdrawal_with_the_setting_off(self):
        rows = [
            _g("adjustment", "staff", 100 * self.M),
            _g("withdrawal", "staff", -40 * self.M, rsn="Mahj", hrsn="Mahj"),
            _g("withdrawal", "staff", -1 * self.M, rsn="Mahj"),
            _g("withdrawal", "coffer", -7 * self.M, rsn="Croc"),
        ]
        out = gbs.roll_up(rows, False, {})
        t = gbs.totals_from_kind_sums(out["kind_sums"])
        assert out["holders"] == [{"player_id": None, "rsn": "Mahj", "held": 40 * self.M}]
        # Unnamed withdrawals are still spent, coffer ones too while it's off.
        assert t["withdrawn"] == 8 * self.M
        assert t["balance"] == 92 * self.M

    def test_explicit_holder_beats_the_withdrawer(self):
        rows = [_g("withdrawal", "coffer", -9, rsn="Alt", hrsn="Main")]
        out = gbs.roll_up(rows, True, {})
        assert [(h["rsn"], h["held"]) for h in out["holders"]] == [("Main", 9)]

    def test_tracked_accounts_use_their_current_name(self):
        rows = [
            _g("withdrawal", "coffer", -9, pid=5, rsn="Old name"),
            _g("payout", "staff", -4, rsn="Winner", hpid=5, hrsn="Old name"),
        ]
        out = gbs.roll_up(rows, True, {5: "New name"})
        assert out["holders"] == [{"player_id": 5, "rsn": "New name", "held": 5}]

    def test_opening_holdings_count_in_the_balance(self):
        rows = [_g("adjustment", "staff", 30, hrsn="Mahj")]
        out = gbs.roll_up(rows, False, {})
        assert out["held"] == 30
        assert gbs.totals_from_kind_sums(out["kind_sums"])["balance"] == 30

    def test_paid_out_everything_stays_listed_and_overdrawn_goes_negative(self):
        rows = [
            _g("withdrawal", "staff", -10, hrsn="Mahj"),
            _g("payout", "staff", -10, rsn="W", hrsn="Mahj"),
            _g("payout", "staff", -3, rsn="W", hrsn="Croc"),
        ]
        out = gbs.roll_up(rows, False, {})
        assert [(h["rsn"], h["held"]) for h in out["holders"]] == [("Mahj", 0), ("Croc", -3)]

    def test_held_moves(self):
        assert gbs.is_held_move("holder_transfer", "staff", True, False)
        assert gbs.is_held_move("withdrawal", "staff", True, False)
        assert gbs.is_held_move("donation", "coffer", True, False)
        assert gbs.is_held_move("withdrawal", "coffer", False, True)
        assert not gbs.is_held_move("donation", "coffer", False, True)
        assert not gbs.is_held_move("payout", "staff", True, True)
        assert not gbs.is_held_move("adjustment", "staff", True, True)

    def test_holder_key_follows_name_equivalence(self):
        assert gbs.holder_key(None, " Iron_Mahj ") == gbs.holder_key(None, "iron-mahj")
        assert gbs.holder_key(7, "anything") == ("p", 7)
        assert gbs.holder_key(None, "  ") is None

    def test_holder_transfer_rules(self):
        gbs.check_holder("holder_transfer", (None, "Croc"), (None, "Mahj"))
        gbs.check_holder("payout", (None, "W"), (None, None))
        with pytest.raises(gbs.BankRuleError):
            gbs.check_holder("holder_transfer", (None, "Croc"), (None, None))
        with pytest.raises(gbs.BankRuleError):
            gbs.check_holder("holder_transfer", (None, "Mahj"), (None, "mahj"))
        assert gbs.signed_amount("holder_transfer", 5) == 5

    def test_totals_never_count_a_hand_over(self):
        t = gbs.totals_from_kind_sums([("donation", 10, 1), ("holder_transfer", 4, 1)])
        assert t["balance"] == 10 and t["adjustments"] == 0


class TestEntryDate:
    NOW = datetime(2026, 11, 2, 21, 0, 0)

    def _ts(self, dt):
        from calendar import timegm

        return timegm(dt.utctimetuple())

    def test_backdated_date_round_trips_as_naive_utc(self):
        when = datetime(2024, 5, 1, 12, 0, 0)
        assert gbs.entry_date(self._ts(when), self.NOW) == when

    def test_clock_slack_is_allowed(self):
        soon = datetime(2026, 11, 2, 21, 5, 0)
        assert gbs.entry_date(self._ts(soon), self.NOW) == soon

    @pytest.mark.parametrize("value", [
        "2024-05-01", 1.5, True, None, 10 ** 15,
    ])
    def test_not_a_timestamp(self, value):
        with pytest.raises(gbs.BankRuleError):
            gbs.entry_date(value, self.NOW)

    def test_future_refused(self):
        with pytest.raises(gbs.BankRuleError):
            gbs.entry_date(self._ts(datetime(2026, 11, 3)), self.NOW)

    def test_before_old_school_refused(self):
        with pytest.raises(gbs.BankRuleError):
            gbs.entry_date(self._ts(datetime(2012, 12, 31)), self.NOW)


class TestCofferRules:
    def test_status_defaults_to_review(self):
        assert gbs.coffer_status("confirmed") == "confirmed"
        assert gbs.coffer_status(" Confirmed ") == "confirmed"
        assert gbs.coffer_status("pending") == "pending"
        for raw in (None, "", "yes", "1"):
            assert gbs.coffer_status(raw) == "pending"

    def test_kinds_map_to_bank_kinds(self):
        assert gbs.COFFER_KINDS == {"coffer_donation": "donation", "coffer_withdrawal": "withdrawal"}

    def test_ref_is_deterministic_and_fits_the_column(self):
        a = gbs.coffer_ref("clan", "X has deposited 5 coins into the coffer.", 100, 1)
        assert a == gbs.coffer_ref("clan", "X has deposited 5 coins into the coffer.", 100, 1)
        assert a.startswith("coffer:") and len(a) <= 80

    def test_ref_differs_by_clan_line_bucket_and_occurrence(self):
        base = gbs.coffer_ref("clan", "m", 100, 1)
        assert gbs.coffer_ref("other", "m", 100, 1) != base
        assert gbs.coffer_ref("clan", "n", 100, 1) != base
        assert gbs.coffer_ref("clan", "m", 101, 1) != base
        # One relayer seeing the same line twice: a second, real deposit.
        assert gbs.coffer_ref("clan", "m", 100, 2) != base

    def test_relays_straddling_a_bucket_edge_collapse(self):
        edge = 100 * gbs.COFFER_BUCKET_SECONDS
        first, _ = gbs.coffer_refs("clan", "m", edge - 1, 1)
        _, seen = gbs.coffer_refs("clan", "m", edge + 2, 1)
        assert first in seen

    def test_occurrence_outlives_the_match_window(self):
        assert gbs.COFFER_OCCURRENCE_TTL_SECONDS >= 2 * gbs.COFFER_BUCKET_SECONDS


# --------------------------------------------------------------------------- #
# Route harness
# --------------------------------------------------------------------------- #
class _S:
    """Scripted session: each query() returns the next batch."""

    def __init__(self, *batches):
        self._batches = list(batches)
        self.added = []
        self.deleted = []
        self.committed = False

    def query(self, *a, **k):
        assert self._batches, "unexpected extra query"
        return _Q(self._batches.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def flush(self):
        for i, obj in enumerate(self.added):
            if getattr(obj, "id", None) is None:
                obj.id = 100 + i

    def commit(self):
        self.committed = True


class _Rec:
    """Recording stand-in for a conftest-mocked ORM class: constructing it
    keeps the kwargs, and class-level attributes let filters reference
    columns (the scripted _Q ignores filter args)."""

    id = group_id = kind = status = player_id = rsn = amount = None
    created_at = event_buyin_id = event_id = source = user_id = None
    note = review_note = proof_url = confirmed_at = event_label = None
    holder_player_id = holder_rsn = None

    def __init__(self, **kw):
        self.id = None
        self.__dict__.update(kw)


class _RecEntry(_Rec):
    pass


class _RecBuyin(_Rec):
    pass


class _RecAudit(_Rec):
    pass


def _entry(**kw):
    base = dict(
        id=9, group_id=42, kind="donation", amount=1_000, status="pending",
        source="member", player_id=None, rsn="Zez", user_id=None, note=None,
        review_note=None, proof_url=None, event_id=None, event_buyin_id=None,
        event_label=None, created_at=None, confirmed_at=None, acted_by_user_id=None,
        holder_player_id=None, holder_rsn=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session, *, admin=True, user_id=7):
    monkeypatch.setattr(gb, "current_user_id", lambda: user_id)
    monkeypatch.setattr(gb, "manageable_guild_ids", lambda uid: set())
    monkeypatch.setattr(gb, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(gb, "GroupBankEntry", _RecEntry)
    monkeypatch.setattr(gb, "EventBuyin", _RecBuyin)
    monkeypatch.setattr(gb, "AuditLog", _RecAudit)
    monkeypatch.setattr(gb, "_match_player", lambda s, rsn: None)
    # Discord side (suggestion #230): record what would be posted.
    session.queued_posts = []
    session.dirty = []
    monkeypatch.setattr(gb, "_queue_posts",
                        lambda s, gid, rows: session.queued_posts.extend(rows))
    monkeypatch.setattr(gb, "_board_dirty", lambda gid: session.dirty.append(gid))

    def _assert_admin(*a, **k):
        if not admin:
            raise ProblemException(403, "Forbidden", "Admin rights on this group are required.")
        return "admin"

    monkeypatch.setattr(gb, "assert_group_admin", _assert_admin)


_GROUP = SimpleNamespace(group_id=42)


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
class TestRecordEntry:
    async def test_donation_by_name_is_confirmed_and_audited(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 5_000_000, "rsn": "Sponsor"},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.amount == 5_000_000 and row.status == "confirmed"
        assert row.source == "staff" and row.confirmed_at is not None
        audit = next(a for a in s.added if isinstance(a, _RecAudit))
        assert audit.action == "group.bank.record"
        assert s.committed

    async def test_withdrawal_stored_negative_and_needs_no_name(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "withdrawal", "amount": 2_000},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.amount == -2_000

    async def test_typed_name_links_a_tracked_account(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        monkeypatch.setattr(
            gb, "_match_player",
            lambda s, rsn: SimpleNamespace(player_id=5, player_name="Zezima", user_id=8),
        )
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 100, "rsn": "zezima"},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert (row.player_id, row.rsn, row.user_id) == (5, "Zezima", 8)

    async def test_payout_needs_a_recipient(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries", json={"kind": "payout", "amount": 2_000},
        )
        assert r.status_code == 422
        assert not s.committed

    async def test_pending_is_not_stamped_confirmed(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 10, "rsn": "x", "status": "pending"},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.status == "pending" and row.confirmed_at is None

    @pytest.mark.parametrize("body", [
        {"kind": "event_transfer", "amount": 10, "event_id": 1},   # has its own route
        {"kind": "donation", "amount": 0, "rsn": "x"},
        {"kind": "donation", "amount": 10, "rsn": "x", "status": "void"},
        {"kind": "donation", "amount": 10, "rsn": "x", "proof_key": "https://evil.example/a.png"},
    ])
    async def test_rejected_before_touching_the_db(self, client, monkeypatch, body):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/entries", json=body)
        assert r.status_code == 422

    async def test_non_admin_forbidden(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s, admin=False)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 10, "rsn": "x"},
        )
        assert r.status_code == 403
        assert not s.committed

    async def test_backdated_entry_keeps_its_date(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 10, "rsn": "x", "created_at": 1_714_564_800},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.created_at == datetime(2024, 5, 1, 12, 0, 0)
        # Confirmed now, whatever day the GP moved.
        assert row.confirmed_at > row.created_at

    async def test_undated_entry_is_dated_now(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        await client.post(
            "/api/v1/groups/42/bank/entries", json={"kind": "donation", "amount": 10, "rsn": "x"},
        )
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.created_at is not None and row.created_at == row.confirmed_at

    async def test_future_date_rejected_before_touching_the_db(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 10, "rsn": "x", "created_at": 4_102_444_800},
        )
        assert r.status_code == 422

    async def test_event_must_belong_to_group(self, client, monkeypatch):
        s = _S([_GROUP], [_event(group_id=99)])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "payout", "amount": 10, "rsn": "x", "event_id": 1},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# update / review
# --------------------------------------------------------------------------- #
class TestHolderWrites:
    """Suggestion #160: recording who holds the GP."""

    async def test_withdrawal_into_someones_keeping(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "withdrawal", "amount": 40_000_000, "rsn": "Mahj", "holder_rsn": "Mahj"},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.amount == -40_000_000 and row.holder_rsn == "Mahj"
        assert '"holder_rsn": "Mahj"' in next(a for a in s.added if isinstance(a, _RecAudit)).after

    async def test_holder_transfer_needs_both_people(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "holder_transfer", "amount": 10, "rsn": "Croc"},
        )
        assert r.status_code == 422 and not s.committed
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "holder_transfer", "amount": 10, "holder_rsn": "Mahj"},
        )
        assert r.status_code == 422 and not s.committed

    async def test_holder_transfer_to_the_same_person_refused(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "holder_transfer", "amount": 10, "rsn": "Mahj", "holder_rsn": "mahj"},
        )
        assert r.status_code == 422

    async def test_holder_transfer_stored_positive(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "holder_transfer", "amount": 10, "rsn": "Croc", "holder_rsn": "Mahj"},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert (row.amount, row.rsn, row.holder_rsn) == (10, "Croc", "Mahj")

    async def test_typed_holder_links_a_tracked_account(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        monkeypatch.setattr(
            gb, "_match_player",
            lambda s, rsn: SimpleNamespace(player_id=5, player_name="Mahj", user_id=None)
            if rsn == "mahj" else None,
        )
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "payout", "amount": 3, "rsn": "Winner", "holder_rsn": "mahj"},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert (row.holder_player_id, row.holder_rsn) == (5, "Mahj")

    async def test_mark_a_deposit_as_returned_and_undo_it(self, client, monkeypatch):
        row = _entry(status="confirmed", source="coffer", rsn="Croc", confirmed_at=datetime(2026, 11, 7))
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"holder_rsn": "Croc"})
        assert r.status_code == 200, await r.get_data()
        assert row.holder_rsn == "Croc" and row.holder_player_id is None
        # Editing doesn't post (only a first confirmation does).
        assert s.queued_posts == []
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9",
                               json={"holder_rsn": None, "holder_player_id": None})
        assert r.status_code == 200
        assert row.holder_rsn is None

    async def test_a_hand_over_cant_lose_its_holder(self, client, monkeypatch):
        row = _entry(kind="holder_transfer", amount=10, status="confirmed", rsn="Croc",
                     holder_rsn="Mahj")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"holder_rsn": ""})
        assert r.status_code == 422 and not s.committed

    async def test_pot_transfer_records_who_paid(self, client, monkeypatch):
        import web_api.routes.events as evr

        monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
        s = _S([_GROUP], [_event(group_id=42, buyins_enabled=True)])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/transfer",
            json={"event_id": 1, "amount": 7_500, "holder_rsn": "Mahj"},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.holder_rsn == "Mahj" and row.amount == -7_500

    async def test_pot_transfer_holder_is_editable(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-5, status="confirmed", rsn=None,
                     event_id=1, event_buyin_id=3)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"holder_rsn": "Mahj"})
        assert r.status_code == 200, await r.get_data()
        assert row.holder_rsn == "Mahj"

    async def test_outside_event_records_who_paid(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/transfer",
            json={"event_name": "Weekly PvM", "amount": 2, "holder_rsn": "Croc"},
        )
        assert r.status_code == 200, await r.get_data()
        row = next(a for a in s.added if isinstance(a, _RecEntry))
        assert row.holder_rsn == "Croc" and row.event_label == "Weekly PvM"


class TestUpdateEntry:
    async def test_confirm_pending_stamps_confirmed_at(self, client, monkeypatch):
        row = _entry()
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"status": "confirmed"})
        assert r.status_code == 200
        assert row.status == "confirmed" and row.confirmed_at is not None
        assert row.acted_by_user_id == 7

    async def test_reject_with_reason(self, client, monkeypatch):
        row = _entry()
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/groups/42/bank/entries/9",
            json={"status": "rejected", "review_note": "No screenshot of the trade"},
        )
        assert r.status_code == 200
        assert row.status == "rejected" and row.review_note == "No screenshot of the trade"

    async def test_rejected_cannot_jump_to_confirmed(self, client, monkeypatch):
        row = _entry(status="rejected")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"status": "confirmed"})
        assert r.status_code == 409
        assert not s.committed

    async def test_amount_edit_keeps_the_kind_sign(self, client, monkeypatch):
        row = _entry(kind="payout", amount=-100, status="confirmed", rsn="x")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"amount": 250})
        assert r.status_code == 200
        assert row.amount == -250

    async def test_adjustment_edit_can_flip_sign(self, client, monkeypatch):
        row = _entry(kind="adjustment", amount=5_000, status="confirmed", rsn=None)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"amount": -2_000})
        assert r.status_code == 200
        assert row.amount == -2_000

    async def test_rsn_edit_to_free_text_unlinks_the_old_account(self, client, monkeypatch):
        row = _entry(status="confirmed", player_id=5, rsn="Zez", user_id=11)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"rsn": "  Bob  "})
        assert r.status_code == 200
        assert row.player_id is None and row.rsn == "Bob"
        # The submitter is who sent the row in, not the donor; it stays.
        assert row.user_id == 11
        assert s.committed and len(s.added) == 1  # the audit row

    async def test_rsn_edit_links_a_tracked_account(self, client, monkeypatch):
        row = _entry(status="confirmed", player_id=None, rsn="zez typo")
        s = _S([row])
        _wire(monkeypatch, s)
        monkeypatch.setattr(
            gb, "_match_player",
            lambda s, rsn: SimpleNamespace(player_id=5, player_name="Zez", user_id=None),
        )
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"rsn": "zez"})
        assert r.status_code == 200
        assert row.player_id == 5 and row.rsn == "Zez"

    async def test_donation_rsn_cannot_be_cleared(self, client, monkeypatch):
        row = _entry(status="confirmed")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"rsn": ""})
        assert r.status_code == 422
        assert row.rsn == "Zez" and not s.committed

    async def test_transfer_amount_is_fixed(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed", event_buyin_id=3)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"amount": 5})
        assert r.status_code == 409

    async def test_transfer_note_is_editable(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed", event_buyin_id=3)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"note": "Top-up"})
        assert r.status_code == 200 and row.note == "Top-up"

    async def test_date_edit_redates_and_is_audited(self, client, monkeypatch):
        row = _entry(status="confirmed", created_at=datetime(2026, 11, 2, 21, 0, 0))
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/groups/42/bank/entries/9", json={"created_at": 1_714_564_800},
        )
        assert r.status_code == 200, await r.get_data()
        assert row.created_at == datetime(2024, 5, 1, 12, 0, 0)
        audit = next(a for a in s.added if isinstance(a, _RecAudit))
        assert '"created_at": 1714564800' in audit.after
        assert '"created_at": 1714564800' not in audit.before

    async def test_outside_transfer_amount_date_and_name_are_editable(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed",
                     source="staff", rsn=None, event_label="Weekly PvM",
                     created_at=datetime(2026, 11, 2, 21, 0, 0))
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/groups/42/bank/entries/9",
            json={"amount": 250, "created_at": 1_714_564_800, "event_name": " Friday race "},
        )
        assert r.status_code == 200, await r.get_data()
        assert row.amount == -250 and row.event_label == "Friday race"
        assert row.created_at == datetime(2024, 5, 1, 12, 0, 0)

    @pytest.mark.parametrize("patch", [{"status": "pending"}, {"rsn": "Zez"}, {"event_name": ""}])
    async def test_outside_transfer_refuses(self, client, monkeypatch, patch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed", event_label="W")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json=patch)
        assert r.status_code in (409, 422)
        assert row.event_label == "W" and row.status == "confirmed" and not s.committed

    async def test_pot_transfer_cant_be_renamed(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed",
                     event_id=1, event_buyin_id=3)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"event_name": "X"})
        assert r.status_code == 409

    async def test_only_a_transfer_has_an_event_name(self, client, monkeypatch):
        s = _S([_entry(status="confirmed")])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"event_name": "X"})
        assert r.status_code == 422

    async def test_transfer_date_is_fixed(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-100, status="confirmed", event_buyin_id=3)
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch(
            "/api/v1/groups/42/bank/entries/9", json={"created_at": 1_714_564_800},
        )
        assert r.status_code == 409

    async def test_void_row_is_frozen(self, client, monkeypatch):
        row = _entry(status="void", confirmed_at=datetime.utcnow())
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"note": "x"})
        assert r.status_code == 409

    async def test_missing_entry_404(self, client, monkeypatch):
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"note": "x"})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Discord hooks (suggestion #230)
# --------------------------------------------------------------------------- #
class TestDiscordHooks:
    async def test_confirmed_record_is_queued_and_marks_the_board(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 5_000_000, "rsn": "Sponsor"},
        )
        assert r.status_code == 200
        (row,) = s.queued_posts
        assert row.kind == "donation" and row.status == "confirmed"
        assert s.dirty == [42]

    async def test_pending_record_is_not_posted(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        await client.post(
            "/api/v1/groups/42/bank/entries",
            json={"kind": "donation", "amount": 10, "rsn": "x", "status": "pending"},
        )
        assert s.queued_posts == [] and s.dirty == [42]

    async def test_first_confirmation_posts(self, client, monkeypatch):
        row = _entry()
        s = _S([row])
        _wire(monkeypatch, s)
        await client.patch("/api/v1/groups/42/bank/entries/9", json={"status": "confirmed"})
        assert s.queued_posts == [row]

    async def test_reconfirming_a_taken_back_entry_does_not_post_again(self, client, monkeypatch):
        row = _entry(status="pending", confirmed_at=datetime(2026, 11, 1))
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/groups/42/bank/entries/9", json={"status": "confirmed"})
        assert r.status_code == 200
        assert s.queued_posts == [] and s.dirty == [42]

    async def test_plain_edit_does_not_post(self, client, monkeypatch):
        row = _entry(status="confirmed", confirmed_at=datetime(2026, 11, 1))
        s = _S([row])
        _wire(monkeypatch, s)
        await client.patch("/api/v1/groups/42/bank/entries/9", json={"note": "fixed"})
        assert s.queued_posts == [] and s.dirty == [42]


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
class TestDeleteEntry:
    async def test_never_counted_is_hard_deleted(self, client, monkeypatch):
        row = _entry(status="pending")
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/groups/42/bank/entries/9")
        assert r.status_code == 200
        assert (await r.get_json())["voided"] is False
        assert s.deleted == [row]

    async def test_confirmed_is_voided(self, client, monkeypatch):
        row = _entry(status="confirmed", confirmed_at=datetime.utcnow())
        s = _S([row])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/groups/42/bank/entries/9")
        assert (await r.get_json())["voided"] is True
        assert row.status == "void" and not s.deleted

    async def test_transfer_voids_its_pot_row(self, client, monkeypatch):
        row = _entry(kind="event_transfer", amount=-500, status="confirmed",
                     confirmed_at=datetime.utcnow(), event_id=1, event_buyin_id=3)
        buyin = SimpleNamespace(id=3, event_id=1, status="paid", amount=500, acted_by_user_id=None)
        s = _S([row], [buyin])
        _wire(monkeypatch, s)
        bumped = []
        import web_api.routes.events as evr
        monkeypatch.setattr(evr, "_bump", lambda eid=None: bumped.append(eid))
        r = await client.delete("/api/v1/groups/42/bank/entries/9")
        assert r.status_code == 200
        assert row.status == "void" and buyin.status == "void"
        assert bumped == [1]


# --------------------------------------------------------------------------- #
# transfer
# --------------------------------------------------------------------------- #
class TestTransfer:
    def _wire_events(self, monkeypatch):
        import web_api.routes.events as evr
        monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)

    async def test_writes_both_rows(self, client, monkeypatch):
        s = _S([_GROUP], [_event(group_id=42, buyins_enabled=True)])
        _wire(monkeypatch, s)
        self._wire_events(monkeypatch)
        r = await client.post(
            "/api/v1/groups/42/bank/transfer", json={"event_id": 1, "amount": 7_500, "note": "BOTW"},
        )
        assert r.status_code == 200, await r.get_data()
        buyin = next(a for a in s.added if isinstance(a, _RecBuyin))
        entry = next(a for a in s.added if isinstance(a, _RecEntry))
        assert buyin.kind == "donation" and buyin.status == "paid" and buyin.amount == 7_500
        assert buyin.player_id is None and buyin.rsn == gb.TRANSFER_DONOR_LABEL
        assert entry.kind == "event_transfer" and entry.amount == -7_500
        assert entry.event_buyin_id == buyin.id and entry.status == "confirmed"
        assert s.committed

    async def test_other_groups_event_refused(self, client, monkeypatch):
        s = _S([_GROUP], [_event(group_id=99, buyins_enabled=True)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/transfer", json={"event_id": 1, "amount": 5})
        assert r.status_code == 404
        assert not s.committed

    async def test_pot_must_be_on(self, client, monkeypatch):
        s = _S([_GROUP], [_event(group_id=42, buyins_enabled=False)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/transfer", json={"event_id": 1, "amount": 5})
        assert r.status_code == 422

    async def test_past_event_refused(self, client, monkeypatch):
        s = _S([_GROUP], [_event(group_id=42, buyins_enabled=True, status="past")])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/transfer", json={"event_id": 1, "amount": 5})
        assert r.status_code == 409

    async def test_bad_amount(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/transfer", json={"event_id": 1, "amount": -5})
        assert r.status_code == 422

    async def test_outside_event_writes_only_the_bank_row(self, client, monkeypatch):
        s = _S([_GROUP])
        _wire(monkeypatch, s)
        bumped = []
        import web_api.routes.events as evr
        monkeypatch.setattr(evr, "_bump", lambda *a, **k: bumped.append(a))
        r = await client.post(
            "/api/v1/groups/42/bank/transfer",
            json={"event_name": " Weekly PvM ", "amount": 2_000_000, "note": "wk 45",
                  "created_at": 1_714_564_800},
        )
        assert r.status_code == 200, await r.get_data()
        assert not any(isinstance(a, _RecBuyin) for a in s.added)
        entry = next(a for a in s.added if isinstance(a, _RecEntry))
        assert entry.kind == "event_transfer" and entry.amount == -2_000_000
        assert entry.event_id is None and entry.event_label == "Weekly PvM"
        assert entry.status == "confirmed" and entry.event_buyin_id is None
        assert entry.created_at == datetime(2024, 5, 1, 12, 0, 0)
        audit = next(a for a in s.added if isinstance(a, _RecAudit))
        assert '"event_label": "Weekly PvM"' in audit.after
        assert s.queued_posts == [entry] and s.dirty == [42] and s.committed
        assert bumped == []

    @pytest.mark.parametrize("body", [
        {"amount": 5},
        {"event_id": 1, "event_name": "Weekly PvM", "amount": 5},
        {"event_name": "   ", "amount": 5},
        # A pot transfer happens when it's made.
        {"event_id": 1, "amount": 5, "created_at": 1_714_564_800},
    ])
    async def test_target_refused(self, client, monkeypatch, body):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/groups/42/bank/transfer", json=body)
        assert r.status_code == 422
        assert not s.committed


# --------------------------------------------------------------------------- #
# public read
# --------------------------------------------------------------------------- #
class TestGetBank:
    def _wire_read(self, monkeypatch, s, *, viewer=None, role=None, show=(True, True), neutral=False):
        _wire(monkeypatch, s)
        # Reads order by real columns (created_at.desc()): keep a mock class.
        monkeypatch.setattr(gb, "GroupBankEntry", MagicMock())
        monkeypatch.setattr(gb, "optional_user_id", lambda: viewer)
        monkeypatch.setattr(gb, "resolve_group_role", lambda *a, **k: role)
        monkeypatch.setattr(gb, "_bank_visibility", lambda s, gid: show)
        monkeypatch.setattr(gb, "_pending_count", lambda s, gid: 4)
        monkeypatch.setattr(gb, "_on_balance", lambda neutral: True)
        monkeypatch.setattr(gb, "_summary", lambda s, gid: {
            "totals": gbs.totals_from_kind_sums([("donation", 9_000, 2), ("payout", -1_000, 1)]),
            "top_donors": [{"player_id": None, "rsn": "Zez", "total": 9_000, "count": 2}],
            "donor_count": 1,
            "coffer_withdrawals_neutral": neutral,
            "held_outside_coffer": 5_000 if neutral else 0,
            "holders": [{"player_id": None, "rsn": "Mahj", "held": 5_000}] if neutral else [],
        })

    async def test_public_headline(self, client, monkeypatch):
        recent = [_entry(status="confirmed", note="staff only")]
        s = _S([_GROUP], recent)
        self._wire_read(monkeypatch, s)
        r = await client.get("/api/v1/groups/42/bank")
        body = await r.get_json()
        assert body["visible"] is True and body["can_manage"] is False
        assert body["balance"]["value"] == 8_000
        assert body["paid_out_total"]["value"] == 1_000
        assert body["payout_total"]["value"] == 1_000
        assert body["withdrawn_total"]["value"] == 0
        assert body["coffer_withdrawals_neutral"] is False
        assert body["held_outside_coffer_total"]["value"] == 0
        assert body["top_donors"][0]["total"]["value"] == 9_000
        # Staff notes and the queue size never reach the public.
        assert body["recent"][0]["note"] is None
        assert body["pending_count"] == 0
        # Who holds the GP is staff-only.
        assert body["holders"] is None

    async def test_neutral_coffer_moves_reported_and_kept_off_the_activity_list(
        self, client, monkeypatch
    ):
        s = _S([_GROUP], [])
        self._wire_read(monkeypatch, s, viewer=7, role="admin", neutral=True)
        clauses = []
        monkeypatch.setattr(gb, "_on_balance", lambda neutral: clauses.append(neutral) or True)
        body = await (await client.get("/api/v1/groups/42/bank")).get_json()
        assert body["coffer_withdrawals_neutral"] is True
        assert body["held_outside_coffer_total"]["value"] == 5_000
        assert body["holders"] == [
            {"player_id": None, "rsn": "Mahj", "held": {"value": 5_000, "value_formatted": body["holders"][0]["held"]["value_formatted"]}}
        ]
        assert clauses == [True]

    async def test_hidden_from_public_when_turned_off(self, client, monkeypatch):
        s = _S([_GROUP])
        self._wire_read(monkeypatch, s, show=(False, True))
        body = await (await client.get("/api/v1/groups/42/bank")).get_json()
        assert body == {"visible": False, "can_manage": False}

    async def test_donors_hidden_but_totals_shown(self, client, monkeypatch):
        s = _S([_GROUP], [])
        self._wire_read(monkeypatch, s, show=(True, False))
        body = await (await client.get("/api/v1/groups/42/bank")).get_json()
        assert body["top_donors"] is None and body["recent"] is None
        assert body["donated_total"]["value"] == 9_000

    async def test_admin_sees_everything_even_when_hidden(self, client, monkeypatch):
        recent = [_entry(status="confirmed", note="staff only")]
        s = _S([_GROUP], recent)
        self._wire_read(monkeypatch, s, viewer=7, role="admin", show=(False, False))
        body = await (await client.get("/api/v1/groups/42/bank")).get_json()
        assert body["can_manage"] is True and body["pending_count"] == 4
        assert body["recent"][0]["note"] == "staff only"


class _QS(_Q):
    def scalar(self):
        return self._r[0] if self._r else None


class _SS(_S):
    def query(self, *a, **k):
        assert self._batches, "unexpected extra query"
        return _QS(self._batches.pop(0))


class TestOnBalanceSQL:
    def _sql(self, monkeypatch, neutral):
        from db.models.group_bank import GroupBankEntry

        monkeypatch.setattr(gb, "GroupBankEntry", GroupBankEntry)
        return str(gb._on_balance(neutral).compile(compile_kwargs={"literal_binds": True}))

    def test_held_moves_left_out_and_coffer_rule_follows_the_setting(self, monkeypatch):
        off, on = self._sql(monkeypatch, False), self._sql(monkeypatch, True)
        for sql in (off, on):
            assert "holder_transfer" in sql and "holder_player_id IS NOT NULL" in sql
        assert "'coffer'" not in off and "'coffer'" in on


class TestSummarySQL:
    async def test_summary_rolls_up_one_grouped_query(self, monkeypatch):
        grouped = [
            _g("donation", "coffer", 10_000, 2, pid=5, rsn="Old"),
            _g("withdrawal", "coffer", -4_000, 1, rsn="Mahj"),
            _g("payout", "staff", -1_000, 1, rsn="W", hrsn="Mahj"),
        ]
        # The grouped rows, then current names for the tracked accounts.
        s = _S(grouped, [(5, "New")])
        monkeypatch.setattr(gb, "GroupBankEntry", MagicMock())
        monkeypatch.setattr(gb, "_coffer_withdrawals_neutral", lambda s, gid: True)
        out = gb._summary(s, 42)
        assert out["coffer_withdrawals_neutral"] is True
        assert out["held_outside_coffer"] == 3_000
        assert out["holders"] == [{"player_id": None, "rsn": "Mahj", "held": 3_000}]
        assert out["totals"]["balance"] == 9_000
        assert out["totals"]["paid_out"] == 1_000
        assert out["top_donors"][0]["rsn"] == "New" and out["donor_count"] == 1


class TestListEntries:
    def _wire_list(self, monkeypatch, s):
        _wire(monkeypatch, s)
        # The ledger orders and pages on columns: real column expressions, so
        # the keyset filter really builds (the scripted query ignores it).
        from sqlalchemy import column

        monkeypatch.setattr(gb, "GroupBankEntry", SimpleNamespace(
            id=column("id"), group_id=column("group_id"), status=column("status"),
            created_at=column("created_at"),
        ))
        monkeypatch.setattr(gb, "_pending_count", lambda s, gid: 0)
        monkeypatch.setattr(gb, "_outside_events", lambda s, gid: [{"name": "Weekly PvM"}])

    async def test_page_continues_from_the_cursor_rows_date(self, client, monkeypatch):
        rows = [_entry(id=5, status="confirmed"), _entry(id=4, status="confirmed")]
        # group, the page query (built first), cursor row's created_at, transfer events
        s = _SS([_GROUP], rows, [datetime(2024, 5, 1)], [])
        self._wire_list(monkeypatch, s)
        r = await client.get("/api/v1/groups/42/bank/entries?before_id=9&limit=5")
        assert r.status_code == 200, await r.get_data()
        body = await r.get_json()
        assert [e["id"] for e in body["entries"]] == [5, 4]
        assert body["next_before_id"] is None
        assert body["outside_events"] == [{"name": "Weekly PvM"}]

    async def test_outside_transfer_row_carries_its_name(self, client, monkeypatch):
        rows = [_entry(id=5, kind="event_transfer", status="confirmed", amount=-10,
                       rsn=None, event_label="Weekly PvM")]
        s = _SS([_GROUP], rows, [])
        self._wire_list(monkeypatch, s)
        r = await client.get("/api/v1/groups/42/bank/entries")
        assert r.status_code == 200, await r.get_data()
        e = (await r.get_json())["entries"][0]
        assert e["event_name"] == "Weekly PvM" and e["outside_event"] is True

    async def test_unknown_cursor_is_422(self, client, monkeypatch):
        s = _SS([_GROUP], [], [])
        self._wire_list(monkeypatch, s)
        r = await client.get("/api/v1/groups/42/bank/entries?before_id=9")
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Prize-pot side of a transfer
# --------------------------------------------------------------------------- #
def _wire_epr(monkeypatch, session):
    monkeypatch.setattr(epr, "current_user_id", lambda: 7)
    monkeypatch.setattr(epr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(epr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(epr, "_is_event_admin", lambda *a, **k: True)
    monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(epr, "AuditLog", _RecAudit)
    monkeypatch.setattr(epr, "EVENT_BUYIN_STATUSES", ("pledged", "paid", "void"))


def _pot_row(**kw):
    base = dict(id=3, event_id=1, team_id=None, player_id=None, rsn="Clan bank", user_id=None,
                kind="donation", amount=500, status="paid", note=None, proof_url=None,
                paid_at=datetime.utcnow(), created_at=None, acted_by_user_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestPotSideOfTransfer:
    async def test_amount_edit_refused_on_bank_funded_donation(self, client, monkeypatch):
        transfer = _entry(id=9, kind="event_transfer", status="confirmed", event_buyin_id=3)
        s = _S([_event()], [_pot_row()], [transfer])
        _wire_epr(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/buyins/3", json={"amount": 1})
        assert r.status_code == 409
        assert not s.committed

    async def test_voiding_the_donation_voids_the_transfer(self, client, monkeypatch):
        transfer = _entry(id=9, kind="event_transfer", status="confirmed", event_buyin_id=3)
        row = _pot_row()
        s = _S([_event()], [row], [transfer])
        _wire_epr(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/buyins/3")
        assert r.status_code == 200
        assert row.status == "void" and transfer.status == "void"

    async def test_ordinary_buyin_skips_the_lookup(self, client, monkeypatch):
        # A player's buy-in is never a transfer: no extra query is issued (the
        # scripted session would raise on one).
        row = _pot_row(player_id=5, kind="buyin")
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/buyins/3", json={"amount": 1})
        assert r.status_code == 200
