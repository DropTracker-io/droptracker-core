"""Unit tests for the prize-pot feature (web52a):

- the pure config parser/normalizer + the pot-summary rollup
  (``web_api/event_prizes.py``), loaded by file path so the stdlib-only helpers
  test in isolation (the ``test_event_leadership`` idiom); and
- the pot routes + the confirm-on-disable PATCH guard, driven through the app
  with the scripted-session harness from ``test_event_auth_modes``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import web_api.routes.event_prizes as epr
import web_api.routes.events as evr
from web_api.common import ProblemException
from tests.unit.test_event_auth_modes import _S, _SessionCM, _event, _player, _team


# Load the pure config module by file path (stdlib-only at import time), so the
# parser/normalizer test never drags in the app package.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web_api", "event_prizes.py",
)
_spec = importlib.util.spec_from_file_location("_event_prizes_under_test", _MODULE_PATH)
ep = importlib.util.module_from_spec(_spec)
sys.modules["_event_prizes_under_test"] = ep
_spec.loader.exec_module(ep)


# --------------------------------------------------------------------------- #
# Pure config: effective_prize_config
# --------------------------------------------------------------------------- #
class TestEffectivePrizeConfig:
    def test_defaults(self):
        assert ep.effective_prize_config(None) == ep.DEFAULT_PRIZE_CONFIG
        # A returned copy — never the shared default dict.
        assert ep.effective_prize_config(None) is not ep.DEFAULT_PRIZE_CONFIG

    def test_overlay_from_json_string(self):
        cfg = ep.effective_prize_config(
            '{"distribution": "top_n", "advertise": true, "show_contributors": false}'
        )
        assert cfg["distribution"] == "top_n"
        assert cfg["advertise"] is True
        assert cfg["show_contributors"] is False

    def test_top_n_clamped_to_team_count(self):
        assert ep.effective_prize_config('{"top_n": 9}', team_count=3)["top_n"] == 3
        # Floor is always 1, even with no team count.
        assert ep.effective_prize_config('{"top_n": 0}')["top_n"] == 1
        # No team count => no upper bound.
        assert ep.effective_prize_config('{"top_n": 9}')["top_n"] == 9

    def test_splits_valid_kept_invalid_dropped(self):
        assert ep.effective_prize_config('{"splits": [60, 30, 10]}')["splits"] == [60, 30, 10]
        # Doesn't sum to 100 -> default.
        assert ep.effective_prize_config('{"splits": [60, 30]}')["splits"] == [100]
        # Contains a non-positive entry -> default.
        assert ep.effective_prize_config('{"splits": [100, 0]}')["splits"] == [100]

    def test_default_buyin_coercion_and_cap(self):
        assert ep.effective_prize_config('{"default_buyin": -5}')["default_buyin"] == 0
        assert ep.effective_prize_config('{"default_buyin": true}')["default_buyin"] == 0
        capped = ep.effective_prize_config('{"default_buyin": %d}' % (10 ** 20))
        assert capped["default_buyin"] == ep.MAX_BUYIN_AMOUNT - 1

    def test_corrupt_or_unknown_falls_back(self):
        assert ep.effective_prize_config("{not json") == ep.DEFAULT_PRIZE_CONFIG
        assert ep.effective_prize_config([1, 2]) == ep.DEFAULT_PRIZE_CONFIG
        assert ep.effective_prize_config('{"distribution": "coup"}')["distribution"] == "first_only"


class TestNormalizePrizeInput:
    def test_partial_object(self):
        assert ep.normalize_prize_input({"advertise": True, "top_n": 2}) == {
            "advertise": True, "top_n": 2}

    def test_rejects_bad_distribution(self):
        assert ep.normalize_prize_input({"distribution": "monarchy"}) is None

    def test_rejects_bad_splits(self):
        assert ep.normalize_prize_input({"splits": [50, 40]}) is None
        assert ep.normalize_prize_input({"splits": [50, 50]}) == {"splits": [50, 50]}

    def test_rejects_amount_at_or_over_cap(self):
        assert ep.normalize_prize_input({"default_buyin": ep.MAX_BUYIN_AMOUNT}) is None
        assert ep.normalize_prize_input(
            {"default_buyin": ep.MAX_BUYIN_AMOUNT - 1}
        ) == {"default_buyin": ep.MAX_BUYIN_AMOUNT - 1}

    def test_rejects_non_bool_flags(self):
        assert ep.normalize_prize_input({"advertise": "yes"}) is None

    def test_rejects_non_dict(self):
        assert ep.normalize_prize_input("advertise") is None


# --------------------------------------------------------------------------- #
# pot_summary rollup
# --------------------------------------------------------------------------- #
class TestPotSummary:
    def test_disabled_short_circuits_without_querying(self):
        # An empty script: any query would raise — a disabled pot must not read
        # the buy-ins table.
        out = ep.pot_summary(_S(), _event(buyins_enabled=False))
        assert out == {
            "enabled": False, "total": 0, "advertise": False,
            "distribution": "first_only", "top_n": 1, "per_team": {},
        }

    def test_enabled_sums_paid_per_team(self):
        rows = [(None, 100), (1, 50), (1, 25), (2, 10)]  # (team_id, amount) paid rows
        out = ep.pot_summary(_S(rows), _event(buyins_enabled=True))
        assert out["enabled"] is True
        assert out["total"] == 185
        assert out["per_team"] == {1: 75, 2: 10}


# --------------------------------------------------------------------------- #
# Route harness
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


class _RecBuyin:
    """Recording stand-in for the conftest-mocked EventBuyin ORM class (so the
    handler's ``row.id`` is a real int the response can serialize). Class-level
    column stand-ins let the routes reference ``EventBuyin.team_id`` etc. in
    filters (the scripted _Q ignores the filter args)."""

    team_id = player_id = event_id = kind = status = amount = None

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = 55


def _wire_epr(monkeypatch, session, *, user_id=7, admin=True):
    monkeypatch.setattr(epr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(epr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(epr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(epr, "_is_event_admin", lambda *a, **k: admin)
    monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
    # The conftest stubs `db` with a MagicMock, so the enum tuples imported
    # `from db` are mocks (``"paid" in <MagicMock>`` is False). Restore the real
    # tuples so the route's membership validation behaves like production.
    monkeypatch.setattr(epr, "EVENT_BUYIN_KINDS", ("buyin", "donation"))
    monkeypatch.setattr(epr, "EVENT_BUYIN_STATUSES", ("pledged", "paid", "void"))


def _buyin(**kw):
    base = dict(
        id=9, event_id=1, team_id=3, player_id=5, rsn="P", user_id=7,
        kind="buyin", amount=1000, status="pledged", note=None,
        paid_at=None, created_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# record_buyin
# --------------------------------------------------------------------------- #
class TestRecordBuyin:
    async def test_donation_with_player_records_paid(self, client, monkeypatch):
        s = _S([_event()], [_team(3)], [_player(player_id=5)])
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post(
            "/api/v1/events/1/buyins",
            json={"kind": "donation", "amount": 1000, "player_id": 5, "team_id": 3},
        )
        assert r.status_code == 200
        assert (await r.get_json())["id"] == 55
        assert s.committed
        row = next(a for a in s.added if isinstance(a, _RecBuyin))
        assert row.status == "paid"        # donations default paid
        assert row.rsn == "P"              # snapshot from the player
        assert row.paid_at is not None

    async def test_buyin_defaults_pledged(self, client, monkeypatch):
        s = _S([_event()], [_team(3)], [_player(player_id=5)])
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post(
            "/api/v1/events/1/buyins",
            json={"amount": 500, "player_id": 5, "team_id": 3},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecBuyin))
        assert row.status == "pledged"
        assert row.paid_at is None

    async def test_external_donor_requires_a_label(self, client, monkeypatch):
        s = _S([_event()])
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post(
            "/api/v1/events/1/buyins", json={"kind": "donation", "amount": 1000}
        )
        assert r.status_code == 422
        assert not s.committed

    async def test_external_donor_free_text_rsn_ok(self, client, monkeypatch):
        s = _S([_event()])
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post(
            "/api/v1/events/1/buyins",
            json={"kind": "donation", "amount": 1000, "rsn": "Sponsor"},
        )
        assert r.status_code == 200
        row = next(a for a in s.added if isinstance(a, _RecBuyin))
        assert row.player_id is None and row.rsn == "Sponsor"

    async def test_amount_out_of_range_rejected(self, client, monkeypatch):
        s = _S([_event()])
        _wire_epr(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/buyins",
            json={"kind": "donation", "amount": ep.MAX_BUYIN_AMOUNT, "rsn": "x"},
        )
        assert r.status_code == 422

    async def test_negative_amount_rejected(self, client, monkeypatch):
        s = _S([_event()])
        _wire_epr(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/buyins", json={"amount": -5, "rsn": "x"}
        )
        assert r.status_code == 422

    async def test_non_admin_without_leader_mark_forbidden(self, client, monkeypatch):
        s = _S([_event(prize_config=None)])
        _wire_epr(monkeypatch, s, admin=False)
        r = await client.post(
            "/api/v1/events/1/buyins", json={"amount": 500, "rsn": "x"}
        )
        assert r.status_code == 403
        assert not s.committed


# --------------------------------------------------------------------------- #
# update_buyin (the "tick")
# --------------------------------------------------------------------------- #
class TestUpdateBuyin:
    async def test_mark_paid_stamps_paid_at(self, client, monkeypatch):
        row = _buyin(status="pledged", paid_at=None)
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.patch(
            "/api/v1/events/1/buyins/9", json={"status": "paid", "amount": 5000}
        )
        assert r.status_code == 200
        assert row.status == "paid"
        assert row.amount == 5000
        assert row.paid_at is not None
        assert s.committed

    async def test_unpaying_clears_paid_at(self, client, monkeypatch):
        row = _buyin(status="paid", paid_at=datetime.utcnow())
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.patch(
            "/api/v1/events/1/buyins/9", json={"status": "pledged"}
        )
        assert r.status_code == 200
        assert row.status == "pledged" and row.paid_at is None

    async def test_bad_status_rejected(self, client, monkeypatch):
        row = _buyin()
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.patch(
            "/api/v1/events/1/buyins/9", json={"status": "banana"}
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# delete_buyin
# --------------------------------------------------------------------------- #
class TestDeleteBuyin:
    async def test_paid_row_is_soft_voided(self, client, monkeypatch):
        row = _buyin(status="paid", paid_at=datetime.utcnow())
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/buyins/9")
        assert r.status_code == 200
        assert (await r.get_json())["voided"] is True
        assert row.status == "void"   # kept for audit / to restore on re-enable

    async def test_pledged_row_is_hard_deleted(self, client, monkeypatch):
        row = _buyin(status="pledged", paid_at=None)
        s = _S([_event()], [row])
        _wire_epr(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/buyins/9")
        assert r.status_code == 200
        assert (await r.get_json())["voided"] is False


# --------------------------------------------------------------------------- #
# bulk_seed_buyins
# --------------------------------------------------------------------------- #
class TestBulkSeed:
    async def test_seeds_one_pledged_row_per_member(self, client, monkeypatch):
        members = [(1, 5, "Alice", 7), (1, 6, "Bob", 8)]  # (team, player, rsn, uid)
        existing = []  # nobody seeded yet
        s = _S([_event(prize_config='{"default_buyin": 5000000}')], members, existing)
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post("/api/v1/events/1/buyins/bulk", json={})
        assert r.status_code == 200
        assert (await r.get_json())["created"] == 2
        seeded = [a for a in s.added if isinstance(a, _RecBuyin)]
        assert len(seeded) == 2
        assert all(row.status == "pledged" and row.amount == 5000000 for row in seeded)

    async def test_skips_already_seeded_members(self, client, monkeypatch):
        members = [(1, 5, "Alice", 7), (1, 6, "Bob", 8)]
        existing = [(1, 5)]  # Alice already has a buy-in
        s = _S([_event(prize_config=None)], members, existing)
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "EventBuyin", _RecBuyin)
        r = await client.post("/api/v1/events/1/buyins/bulk", json={})
        assert r.status_code == 200
        assert (await r.get_json())["created"] == 1  # only Bob


# --------------------------------------------------------------------------- #
# announce_pot
# --------------------------------------------------------------------------- #
class TestAnnounce:
    async def test_disabled_pot_rejected(self, client, monkeypatch):
        s = _S([_event(buyins_enabled=False)])
        _wire_epr(monkeypatch, s)
        r = await client.post("/api/v1/events/1/pot/announce", json={})
        assert r.status_code == 422
        assert not s.committed

    async def test_no_channel_rejected(self, client, monkeypatch):
        s = _S([_event(buyins_enabled=True)], [])  # no announcements channel
        _wire_epr(monkeypatch, s)
        r = await client.post("/api/v1/events/1/pot/announce", json={})
        assert r.status_code == 422

    async def test_enqueues_event_pot_notification(self, client, monkeypatch):
        s = _S(
            [_event(buyins_enabled=True)],
            [SimpleNamespace(id=1)],       # announcements channel exists
            [(1,), (2,)],                   # team_count = 2
            [(None, 250_000_000)],          # paid pot rows
        )
        _wire_epr(monkeypatch, s)
        monkeypatch.setattr(epr, "_representative_player_id", lambda _s, _eid: 7)
        r = await client.post("/api/v1/events/1/pot/announce", json={})
        assert r.status_code == 200
        assert s.committed
        # A queue row + an audit row were added.
        assert len(s.added) == 2


# --------------------------------------------------------------------------- #
# _pot_payload — show_contributors redaction + per-team totals
# --------------------------------------------------------------------------- #
class TestPotRedaction:
    def _rows(self):
        return [
            _buyin(id=1, team_id=1, player_id=5, kind="buyin", amount=100, status="paid"),
            _buyin(id=2, team_id=1, player_id=6, kind="buyin", amount=50, status="pledged"),
            _buyin(id=3, team_id=2, player_id=None, rsn="Sponsor", kind="donation",
                   amount=25, status="paid"),
        ]

    def test_public_hidden_when_show_contributors_false(self, monkeypatch):
        monkeypatch.setattr(epr, "_is_event_admin", lambda *a, **k: False)
        ev = _event(buyins_enabled=True, prize_config='{"show_contributors": false}')
        s = _S([_team(1), _team(2)], [(1, 2), (2, 1)], self._rows(), [(5, "Zez")])
        out = epr._pot_payload(s, ev, viewer_id=None)
        assert out["contributors"] is None
        assert out["total"]["value"] == 125          # only paid rows (100 + 25)
        per_team = {t["team_id"]: t["total"]["value"] for t in out["per_team"]}
        assert per_team == {1: 100, 2: 25}           # pledged 50 excluded
        assert out["can_manage"] is False

    def test_admin_sees_all_rows_including_pledged(self, monkeypatch):
        monkeypatch.setattr(epr, "_is_event_admin", lambda *a, **k: True)
        ev = _event(buyins_enabled=True, prize_config='{"show_contributors": false}')
        s = _S([_team(1), _team(2)], [(1, 2), (2, 1)], self._rows(), [(5, "Zez")])
        out = epr._pot_payload(s, ev, viewer_id=7)
        assert out["contributors"] is not None
        assert len(out["contributors"]) == 3         # admin sees the pledged one too
        # Live player name wins over the stored snapshot.
        assert out["contributors"][0]["rsn"] == "Zez"
        assert out["can_manage"] is True


# --------------------------------------------------------------------------- #
# Confirm-on-disable guard (events.py PATCH)
# --------------------------------------------------------------------------- #
def _wire_evr(monkeypatch, session, *, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_detail", lambda s, ev, viewer_id=None: {"id": ev.id})


class TestConfirmOnDisable:
    async def test_disable_with_records_needs_confirmation(self, client, monkeypatch):
        s = _S([_event(status="draft", buyins_enabled=True)], [(3, 250_000_000)])
        _wire_evr(monkeypatch, s)
        r = await client.patch("/api/v1/events/1", json={"buyins_enabled": False})
        assert r.status_code == 409
        body = await r.get_json()
        assert body["type"] == "buyins-present"
        assert body["count"] == 3 and body["total"] == 250_000_000
        assert not s.committed

    async def test_disable_with_confirm_flag_succeeds(self, client, monkeypatch):
        ev = _event(status="draft", buyins_enabled=True)
        s = _S([ev], [(3, 250_000_000)])
        _wire_evr(monkeypatch, s)
        r = await client.patch(
            "/api/v1/events/1",
            json={"buyins_enabled": False, "confirm_disable_buyins": True},
        )
        assert r.status_code == 200
        assert ev.buyins_enabled is False
        assert s.committed

    async def test_enable_never_reads_the_ledger(self, client, monkeypatch):
        # Only the event row is scripted — a stray stats query would misalign
        # and raise. Enabling must not touch web_event_buyins.
        ev = _event(buyins_enabled=False)
        s = _S([ev])
        _wire_evr(monkeypatch, s)
        r = await client.patch("/api/v1/events/1", json={"buyins_enabled": True})
        assert r.status_code == 200
        assert ev.buyins_enabled is True

    async def test_invalid_prize_config_rejected(self, client, monkeypatch):
        s = _S([_event()])
        _wire_evr(monkeypatch, s)
        r = await client.patch(
            "/api/v1/events/1", json={"prize_config": {"distribution": "coup"}}
        )
        assert r.status_code == 422
        assert not s.committed
