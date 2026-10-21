"""Self-serve data removal (web137a): anonymized posts, name checks, decisions."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


anon = _load("discord_anonymize_under_test", "services/discord_anonymize.py")

# tests/conftest.py stubs ``db``; give the service the real status constants.
_consts = types.ModuleType("db.models.data_removal")
for k, v in dict(KIND_PLAYER="player", KIND_GROUP="group", AWAITING="awaiting_verification",
                 SCHEDULED="scheduled", COMPLETED="completed", CANCELLED="cancelled",
                 REJECTED="rejected", FAILED="failed").items():
    setattr(_consts, k, v)
_consts.OPEN_STATUSES = ("awaiting_verification", "scheduled")
_consts.DataRemovalRequest = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
_saved = sys.modules.get("db.models.data_removal")
sys.modules["db.models.data_removal"] = _consts
try:
    dr = _load("data_removal_under_test", "services/data_removal.py")
finally:
    if _saved is not None:
        sys.modules["db.models.data_removal"] = _saved
    else:
        sys.modules.pop("db.models.data_removal", None)


# --------------------------------------------------------------------------- #
class TestNamePattern:
    def test_osrs_separators_are_equivalent(self):
        pat = anon.name_pattern(["Of Fury"])
        for s in ("Of Fury", "of_fury", "OF-FURY"):
            assert pat.search(f"x {s} y")

    def test_word_bounded(self):
        pat = anon.name_pattern(["Fury"])
        assert not pat.search("Furyous got a drop")
        assert pat.search("Fury got a drop")

    def test_no_names(self):
        assert anon.name_pattern(["", None, "  "]) is None


class TestEmbedMessage:
    MSG = {
        "content": "",
        "flags": 0,
        "attachments": [{"id": "1", "filename": "image.png"}],
        "embeds": [{
            "type": "rich",
            "title": "Of Fury received a drop!",
            "description": "[Of Fury](https://www.droptracker.io/players/5756157) got **Bird nest**",
            "fields": [{"name": "Player", "value": "Of_Fury", "inline": True},
                       {"name": "Value", "value": "3,840", "inline": True}],
            "author": {"name": "Of Fury", "url": "https://x/players/1", "proxy_icon_url": "p"},
            "footer": {"text": "DropTracker | Of Fury"},
            "image": {"url": "attachment://image.png", "proxy_url": "p", "width": 1},
            "thumbnail": {"url": "https://www.droptracker.io/img/itemdb/5075.png", "width": 2},
        }],
    }

    def test_names_replaced_and_screenshot_removed(self):
        body = anon.anonymize_payload(self.MSG, ["Of Fury"])
        e = body["embeds"][0]
        assert "Fury" not in str(e)
        assert e["title"] == "*Deleted user* received a drop!"
        # The profile link goes with the name.
        assert e["description"].startswith("*Deleted user* got")
        assert "image" not in e  # the uploaded screenshot
        assert e["thumbnail"] == {"url": "https://www.droptracker.io/img/itemdb/5075.png"}
        assert "url" not in e["author"]
        assert body["attachments"] == []
        assert "type" not in e

    def test_untouched_post_returns_none(self):
        msg = {"content": "Someone else got a drop", "flags": 0, "embeds": [], "attachments": []}
        assert anon.anonymize_payload(msg, ["Of Fury"]) is None


class TestComponentsV2:
    def test_text_and_media_scrubbed(self):
        msg = {
            "flags": anon.IS_COMPONENTS_V2,
            "attachments": [],
            "components": [{
                "type": 17, "id": 1,
                "components": [
                    {"type": 9, "components": [{"type": 10, "content": "## Of Fury got a pet"}],
                     "accessory": {"type": 11, "media": {
                         "url": "https://www.droptracker.io/img/user-upload/1/pet/a.png",
                         "proxy_url": "p", "width": 3}}},
                    {"type": 12, "items": [{"media": {"url": "attachment://shot.png"}}]},
                    {"type": 10, "content": "Clan total: 5m"},
                ],
            }],
        }
        body = anon.anonymize_payload(msg, ["Of Fury"])
        comps = body["components"][0]["components"]
        # Section without its screenshot is flattened to its text.
        assert comps[0] == {"type": 10, "content": "## *Deleted user* got a pet"}
        # The gallery held only the upload, so it is gone.
        assert [c["type"] for c in comps] == [10, 10]
        assert "content" not in body and "embeds" not in body


# --------------------------------------------------------------------------- #
class TestNames:
    def test_names_match_osrs_equivalence(self):
        assert dr.names_match("of_fury", "Of Fury")
        assert dr.names_match("  OF-FURY ", "Of Fury")
        assert not dr.names_match("Of Fur", "Of Fury")
        assert not dr.names_match("", None)

    def test_phrase_shape(self):
        p = dr.new_phrase()
        assert p.startswith("DropTracker removal ") and len(p.split()[-1]) == 6


class _S:
    """Session stub for the decision functions."""

    def __init__(self, req, hidden=0):
        self.req, self.hidden, self.sql = req, hidden, []

    def query(self, *a, **k):
        req = self.req

        class Q:
            def filter(self, *a, **k):
                return self

            def first(self):
                return req
        return Q()

    def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params))
        return SimpleNamespace(scalar=lambda: self.hidden)

    def add(self, *_):
        pass

    def flush(self):
        pass


def _req(**kw):
    base = dict(id=7, kind="player", subject_id=5, subject_name="Of Fury",
                status="awaiting_verification", requested_by_user_id=0, prior_hidden=None,
                ticket_id=494, verification_phrase="DropTracker removal 123456",
                purge_after=None, created_at=datetime(2026, 10, 20), decided_at=None,
                completed_at=None, decided_by_user_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestDecisions:
    def test_approve_schedules_grace_and_hides_player(self):
        req = _req()
        s = _S(req, hidden=0)
        now = datetime(2026, 10, 20, 12)
        dr.approve(s, 7, 99, now=now)
        assert req.status == "scheduled"
        assert req.purge_after == now + timedelta(days=7)
        assert req.prior_hidden == 0
        assert any("SET hidden = 1" in sql for sql, _ in s.sql)

    def test_cancel_restores_prior_visibility(self):
        req = _req(status="scheduled", prior_hidden=0)
        s = _S(req)
        dr.cancel(s, 7, 0)  # requester is user 0 (a real id)
        assert req.status == "cancelled"
        assert any("SET hidden = :h" in sql and p["h"] == 0 for sql, p in s.sql)

    def test_only_requester_or_staff_can_cancel(self):
        with pytest.raises(dr.RemovalError) as e:
            dr.cancel(_S(_req()), 7, 12345)
        assert e.value.status == 403
        dr.cancel(_S(_req()), 7, 12345, staff=True)

    def test_completed_cannot_be_cancelled_or_reapproved(self):
        for fn in (lambda s: dr.cancel(s, 7, 0), lambda s: dr.approve(s, 7, 1)):
            with pytest.raises(dr.RemovalError) as e:
                fn(_S(_req(status="completed")))
            assert e.value.status == 409


class TestRequests:
    def test_player_must_be_claimed_by_requester(self):
        player = SimpleNamespace(player_id=5, user_id=1, player_name="Of Fury")
        s = SimpleNamespace(query=lambda *a: SimpleNamespace(
            filter=lambda *a: SimpleNamespace(first=lambda: player)))
        with pytest.raises(dr.RemovalError) as e:
            dr.request_player_removal(s, SimpleNamespace(user_id=2), 5, "Of Fury")
        assert e.value.status == 404

    def test_player_name_must_match(self):
        player = SimpleNamespace(player_id=5, user_id=2, player_name="Of Fury")
        s = SimpleNamespace(query=lambda *a: SimpleNamespace(
            filter=lambda *a: SimpleNamespace(first=lambda: player)))
        with pytest.raises(dr.RemovalError) as e:
            dr.request_player_removal(s, SimpleNamespace(user_id=2), 5, "Of Furry")
        assert e.value.status == 422

    def test_global_group_refused(self):
        group = SimpleNamespace(group_id=2, group_name="DropTracker.io")
        s = SimpleNamespace(query=lambda *a: SimpleNamespace(
            filter=lambda *a: SimpleNamespace(first=lambda: group)))
        with pytest.raises(dr.RemovalError):
            dr.request_group_removal(s, SimpleNamespace(user_id=1), 2, "DropTracker.io",
                                     is_owner=True)

    def test_group_needs_owner(self):
        group = SimpleNamespace(group_id=9, group_name="Renatus")
        s = SimpleNamespace(query=lambda *a: SimpleNamespace(
            filter=lambda *a: SimpleNamespace(first=lambda: group)))
        with pytest.raises(dr.RemovalError) as e:
            dr.request_group_removal(s, SimpleNamespace(user_id=1), 9, "Renatus", is_owner=False)
        assert e.value.status == 403


class TestStatusPayload:
    def test_phrase_only_for_players(self):
        p = dr.status_payload(_req())
        assert p["verification_phrase"] == "DropTracker removal 123456" and p["can_cancel"]
        g = dr.status_payload(_req(kind="group", verification_phrase="x"))
        assert g["verification_phrase"] is None


class TestTicketCard:
    def _mod(self):
        return _load("data_removal_discord_under_test", "services/data_removal_discord.py")

    def test_player_card_shows_phrase(self):
        m = self._mod()
        dumped = " ".join(m.card_text(dr.status_payload(_req()), []))
        assert "DropTracker removal 123456" in dumped
        assert "whole RuneLite client" in dumped
        assert "request #7" in dumped

    def test_group_card_pings_admins(self):
        m = self._mod()
        dumped = " ".join(m.card_text(dr.status_payload(_req(kind="group", subject_name="Renatus")),
                                      ["111", "222"]))
        assert "<@111> <@222>" in dumped and "Renatus" in dumped

    def test_approve_text_names_purge_time(self):
        m = self._mod()
        text = m.decision_text({"subject_name": "Of Fury", "kind": "player",
                                "purge_after": 1761000000}, "approve", "joel")
        assert "<t:1761000000:F>" in text and "hidden" in text
