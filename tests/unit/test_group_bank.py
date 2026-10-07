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
    note = review_note = proof_url = confirmed_at = None

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
        created_at=None, confirmed_at=None, acted_by_user_id=None,
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


# --------------------------------------------------------------------------- #
# public read
# --------------------------------------------------------------------------- #
class TestGetBank:
    def _wire_read(self, monkeypatch, s, *, viewer=None, role=None, show=(True, True)):
        _wire(monkeypatch, s)
        # Reads order by real columns (created_at.desc()): keep a mock class.
        monkeypatch.setattr(gb, "GroupBankEntry", MagicMock())
        monkeypatch.setattr(gb, "optional_user_id", lambda: viewer)
        monkeypatch.setattr(gb, "resolve_group_role", lambda *a, **k: role)
        monkeypatch.setattr(gb, "_bank_visibility", lambda s, gid: show)
        monkeypatch.setattr(gb, "_pending_count", lambda s, gid: 4)
        monkeypatch.setattr(gb, "_summary", lambda s, gid: {
            "totals": gbs.totals_from_kind_sums([("donation", 9_000, 2), ("payout", -1_000, 1)]),
            "top_donors": [{"player_id": None, "rsn": "Zez", "total": 9_000, "count": 2}],
            "donor_count": 1,
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
        assert body["top_donors"][0]["total"]["value"] == 9_000
        # Staff notes and the queue size never reach the public.
        assert body["recent"][0]["note"] is None
        assert body["pending_count"] == 0

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
