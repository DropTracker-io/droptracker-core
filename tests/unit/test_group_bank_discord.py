"""Clan bank on Discord (suggestion #230): post rules, wording, the board
embed and the outbox queueing in services/group_bank_discord.py."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import services.group_bank_discord as gbd

_NOW = datetime(2026, 11, 5, 12, 0, 0)


# --------------------------------------------------------------------------- #
# Which entries post
# --------------------------------------------------------------------------- #
class TestIsPostable:
    def _ok(self, kind, source="staff", created=_NOW, *, outflows=False, neutral=False,
            holder=False):
        return gbd.is_postable(kind, source, created, _NOW, outflows=outflows, neutral=neutral,
                               has_holder=holder)

    def test_donations_post_by_default(self):
        assert self._ok("donation")
        assert self._ok("donation", "coffer")

    @pytest.mark.parametrize("kind", ["withdrawal", "payout", "event_transfer"])
    def test_outflows_only_with_the_switch(self, kind):
        assert not self._ok(kind)
        assert self._ok(kind, outflows=True)

    def test_adjustments_never_post(self):
        assert not self._ok("adjustment", outflows=True)

    def test_neutral_coffer_withdrawal_does_not_post(self):
        assert not self._ok("withdrawal", "coffer", outflows=True, neutral=True)
        # A withdrawal staff record is still GP leaving.
        assert self._ok("withdrawal", "staff", outflows=True, neutral=True)

    def test_gp_moving_between_coffer_and_holders_does_not_post(self):
        # Suggestion #160: a withdrawal someone holds, a returned deposit, a
        # hand-over between holders. A payout from a holder still posts.
        assert not self._ok("withdrawal", outflows=True, holder=True)
        assert not self._ok("donation", "coffer", holder=True)
        assert not self._ok("holder_transfer", outflows=True, holder=True)
        assert self._ok("payout", outflows=True, holder=True)

    def test_history_typed_in_does_not_post(self):
        assert not self._ok("donation", created=_NOW - timedelta(days=30))
        # A coffer line confirmed a few days after it happened still posts.
        assert self._ok("donation", "coffer", created=_NOW - timedelta(days=3))


# --------------------------------------------------------------------------- #
# Wording
# --------------------------------------------------------------------------- #
class TestWording:
    def test_gp(self):
        assert gbd.gp(-2_000_000) == "2,000,000 gp"
        assert gbd.gp_signed(-5) == "-5 gp"

    @pytest.mark.parametrize("n,expected", [
        (999, "999"), (1_500, "1.5K"), (5_000_000, "5M"), (1_250_000_000, "1.25B"),
    ])
    def test_short_gp(self, n, expected):
        assert gbd.short_gp(n) == expected

    @pytest.mark.parametrize("kind,source,amount,name,expected", [
        ("donation", "staff", 5_000_000, "Zezima", "**Zezima** donated **5,000,000 gp**"),
        ("donation", "coffer", 5_000_000, "Zezima",
         "**Zezima** deposited **5,000,000 gp** into the coffer"),
        ("donation", "staff", 100, None, "A clan member donated **100 gp**"),
        ("withdrawal", "coffer", -2_000, "Treasurer",
         "**Treasurer** withdrew **2,000 gp** from the coffer"),
        ("withdrawal", "staff", -2_000, None, "**2,000 gp** withdrawn"),
        ("payout", "staff", -1_000, "Winner", "**Winner** was paid **1,000 gp**"),
        ("event_transfer", "staff", -3_000, None, "**3,000 gp** moved into an event prize pot"),
        ("adjustment", "staff", -50, None, "**50 gp** taken from the bank"),
    ])
    def test_activity_line(self, kind, source, amount, name, expected):
        assert gbd.activity_line(kind, source, amount, name) == expected

    def test_transfer_names_its_event(self):
        assert gbd.activity_line("event_transfer", "staff", -3_000, None, "BOTW") == (
            "**3,000 gp** moved into the **BOTW** prize pot")
        assert gbd.activity_line("event_transfer", "staff", -3_000, None, "Weekly_PvM",
                                 outside_event=True) == (
            "**3,000 gp** set aside for **Weekly\\_PvM**")

    def test_names_are_markdown_escaped(self):
        assert gbd.activity_line("donation", "staff", 1, "big_boi*") == (
            "**big\\_boi\\*** donated **1 gp**")


class TestEntryEmbed:
    def _embed(self, **kw):
        base = dict(group_id=363, group_name="Venture", kind="donation", source="coffer",
                    amount=5_000_000, name="Zezima", balance=12_000_000, proof_url=None,
                    when=_NOW)
        base.update(kw)
        return gbd.entry_embed(**base)

    def test_donation(self):
        e = self._embed()
        assert e["title"] == "Clan bank donation"
        assert e["url"].endswith("/groups/363#bank")
        assert e["description"] == "**Zezima** deposited **5,000,000 gp** into the coffer."
        assert e["fields"] == [{"name": "Bank balance", "value": "12,000,000 gp", "inline": True}]
        assert e["color"] == gbd._COLOR_IN
        assert e["timestamp"] == "2026-11-05T12:00:00+00:00"
        assert "image" not in e

    def test_outflow_colour_and_negative_balance(self):
        e = self._embed(kind="payout", source="staff", amount=-1, balance=-7)
        assert e["color"] == gbd._COLOR_OUT and e["title"] == "Clan bank"
        assert e["fields"][0]["value"] == "-7 gp"

    def test_proof_screenshot_only_over_https(self):
        assert self._embed(proof_url="https://cdn.example/p.png")["image"] == {
            "url": "https://cdn.example/p.png"}
        assert "image" not in self._embed(proof_url="http://cdn.example/p.png")

    def test_no_em_dashes_in_copy(self):
        e = self._embed()
        assert "—" not in str(e)


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #
def _summary(**kw):
    base = {
        "totals": {"balance": 9_000_000, "donated": 10_000_000, "paid_out": 1_000_000,
                   "by_kind": {"donation": 10_000_000, "payout": -1_000_000}},
        "top_donors": [{"rsn": "Zezima", "total": 7_000_000}, {"rsn": None, "total": 3_000_000}],
        "donor_count": 2,
        "coffer_withdrawals_neutral": False,
        "held_outside_coffer": 0,
    }
    base.update(kw)
    return base


_RECENT = [{"kind": "donation", "source": "coffer", "amount": 5_000_000, "rsn": "Zezima",
            "created_at": 1_790_000_000}]


class TestBoardEmbed:
    def test_full_board(self):
        e = gbd.board_embed(group_id=1, group_name="Venture", summary=_summary(),
                            recent=_RECENT, show_names=True)
        assert e["title"] == "Venture clan bank"
        assert gbd.board_title("Venture Clan") == "Venture Clan bank"
        assert e["description"] == "Balance: **9,000,000 gp**"
        names = [f["name"] for f in e["fields"]]
        assert names == ["Donated", "Taken out", "Donors", "Top donors", "Recent activity"]
        donors = e["fields"][3]["value"]
        assert donors == "1. Zezima: 7M\n2. Unknown: 3M"
        assert e["fields"][4]["value"] == (
            "<t:1790000000:R> **Zezima** deposited **5,000,000 gp** into the coffer")
        assert "timestamp" not in e  # same bank, same embed, no needless edits

    def test_names_hidden(self):
        e = gbd.board_embed(group_id=1, group_name="V", summary=_summary(),
                            recent=_RECENT, show_names=False)
        assert "Top donors" not in [f["name"] for f in e["fields"]]
        assert "Zezima" not in str(e)

    def test_held_outside_coffer_shown_when_neutral(self):
        e = gbd.board_embed(group_id=1, group_name="V",
                            summary=_summary(coffer_withdrawals_neutral=True,
                                             held_outside_coffer=40_000_000),
                            recent=[], show_names=True)
        held = next(f for f in e["fields"] if f["name"] == "Held outside the coffer")
        assert held["value"] == "40,000,000 gp"

    def test_empty_bank(self):
        e = gbd.board_embed(group_id=1, group_name="V",
                            summary=_summary(totals={"by_kind": {}}, top_donors=[],
                                             donor_count=0),
                            recent=[], show_names=True)
        assert e["description"] == "Nothing has been recorded in the bank yet."

    def test_long_lists_fit_a_field(self):
        recent = [dict(_RECENT[0], rsn="X" * 24)] * 200
        e = gbd.board_embed(group_id=1, group_name="V", summary=_summary(),
                            recent=recent, show_names=True)
        assert all(len(f["value"]) <= 1024 for f in e["fields"])

    def test_hash_follows_content_and_channel(self):
        e = gbd.board_embed(group_id=1, group_name="V", summary=_summary(),
                            recent=[], show_names=True)
        assert gbd.board_hash("1", e) == gbd.board_hash("1", dict(e))
        assert gbd.board_hash("1", e) != gbd.board_hash("2", e)
        assert gbd.board_hash("1", e) != gbd.board_hash("1", {**e, "description": "x"})


class TestRefs:
    @pytest.mark.parametrize("raw,expected", [
        ("123:456", ("123", "456")),
        ("", (None, None)),
        (None, (None, None)),
        ("0", (None, None)),
        ("456", (None, None)),
        ("a:b", (None, None)),
    ])
    def test_parse_message_ref(self, raw, expected):
        assert gbd.parse_message_ref(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        (b"42", 42), ("7", 7), (b"nope", None), (None, None),
    ])
    def test_dirty_members(self, raw, expected):
        assert gbd._as_group_id(raw) == expected

    def test_channel_id_from(self):
        assert gbd.channel_id_from("1234567890123456789") == "1234567890123456789"
        assert gbd.channel_id_from("<#1234567890123456789>") == "1234567890123456789"
        assert gbd.channel_id_from("0") is None
        assert gbd.channel_id_from(None) is None
        assert gbd.channel_id_from("general") is None


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, result):
        self.result = result

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self.result

    def all(self):
        return self.result


class _Session:
    """Answers the group-name query, then the player-name query."""

    def __init__(self, group_name="Venture", players=()):
        self._answers = [group_name, list(players)]

    def query(self, *a, **k):
        return _Q(self._answers.pop(0))


def _row(**kw):
    base = dict(id=5, kind="donation", source="coffer", amount=5_000_000, player_id=None,
                rsn="Zezima", proof_url=None, created_at=_NOW, confirmed_at=_NOW,
                event_id=None, event_label=None, holder_player_id=None, holder_rsn=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def queue(monkeypatch):
    import sys

    import utils.group_config as gc

    state = {"config": {"bank_post_channel_id": "1234567890123456789"}, "sent": []}
    monkeypatch.setattr(
        gc, "get_bulk",
        lambda _s, gids, keys: {(gids[0], k): v for k, v in state["config"].items()},
    )
    outbox = SimpleNamespace(enqueue=lambda _s, **kw: state["sent"].append(kw))
    monkeypatch.setitem(sys.modules, "services.discord_outbox", outbox)
    monkeypatch.setattr(gbd, "_balance", lambda _s, gid, neutral: 9_000_000)
    return state


class TestQueueEntryPosts:
    def test_queues_a_donation(self, queue):
        assert gbd.queue_entry_posts(_Session(), 363, [_row()]) == 1
        (sent,) = queue["sent"]
        assert sent["channel_id"] == "1234567890123456789"
        assert sent["commit"] is False  # commits with the entry
        assert sent["ref_type"] == "group_bank_entry" and sent["ref_id"] == 5
        assert sent["embed"]["fields"][0]["value"] == "9,000,000 gp"
        assert "**Zezima**" in sent["embed"]["description"]

    def test_no_channel_no_post(self, queue):
        queue["config"] = {}
        assert gbd.queue_entry_posts(_Session(), 363, [_row()]) == 0
        assert queue["sent"] == []

    def test_tracked_player_shown_under_current_name(self, queue):
        gbd.queue_entry_posts(_Session(players=[(8, "New Name")]), 363,
                              [_row(player_id=8, rsn="Old Name")])
        assert "**New Name**" in queue["sent"][0]["embed"]["description"]

    def test_names_hidden_with_show_donors_off(self, queue):
        queue["config"]["bank_show_donors"] = "false"
        gbd.queue_entry_posts(_Session(), 363, [_row()])
        assert "Zezima" not in str(queue["sent"][0]["embed"])

    def test_returned_deposit_does_not_post(self, queue):
        assert gbd.queue_entry_posts(_Session(), 363, [_row(holder_rsn="Zezima")]) == 0

    def test_outflow_needs_the_switch(self, queue):
        withdrawal = _row(kind="withdrawal", amount=-5)
        assert gbd.queue_entry_posts(_Session(), 363, [withdrawal]) == 0
        queue["config"]["bank_post_outflows"] = "true"
        assert gbd.queue_entry_posts(_Session(), 363, [withdrawal]) == 1

    def test_transfers_name_their_event(self, queue):
        queue["config"]["bank_post_outflows"] = "true"
        outside = _row(kind="event_transfer", source="staff", amount=-5, rsn=None,
                       event_label="Weekly PvM")
        gbd.queue_entry_posts(_Session(), 363, [outside])
        assert "set aside for **Weekly PvM**" in queue["sent"][0]["embed"]["description"]
        # A pot transfer looks its event up (the group name, then event names:
        # no player query without a player).
        pot = _row(id=6, kind="event_transfer", source="staff", amount=-5, rsn=None, event_id=12)
        gbd.queue_entry_posts(_Session(players=[(12, "BOTW")]), 363, [pot])
        assert "the **BOTW** prize pot" in queue["sent"][1]["embed"]["description"]

    def test_never_raises(self, queue, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db went away")

        monkeypatch.setattr(gbd, "_balance", _boom)
        assert gbd.queue_entry_posts(_Session(), 363, [_row()]) == 0
