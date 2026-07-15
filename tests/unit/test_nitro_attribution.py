"""Unit tests for services/nitro_attribution.py.

The test harness (tests/conftest.py) stubs the whole ``services`` package with a
MagicMock, so we load the module directly from its file path to get the real
implementation. ``db.entitlements`` is real-loaded by conftest; ``db.models`` is
a MagicMock, which is exactly what we want for the fake-session reconcile tests.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).resolve().parents[2] / "services" / "nitro_attribution.py"
_spec = importlib.util.spec_from_file_location("services.nitro_attribution", _PATH)
nitro = importlib.util.module_from_spec(_spec)
sys.modules["services.nitro_attribution"] = nitro
_spec.loader.exec_module(nitro)


# --------------------------------------------------------------------------- #
# fetch_booster_discord_ids — Discord member enumeration (pure async, no DB)
# --------------------------------------------------------------------------- #
class _FakeHttp:
    def __init__(self, pages):
        self._pages = list(pages)
        self.after_calls = []

    async def list_members(self, guild_id, limit, after=None):
        self.after_calls.append(after)
        return self._pages.pop(0) if self._pages else []


def _member(uid, boosting):
    return {
        "user": {"id": uid},
        "premium_since": "2024-01-01T00:00:00+00:00" if boosting else None,
    }


def test_fetch_boosters_filters_and_paginates():
    page1 = [_member("1", True), _member("2", False)]  # len == limit -> continue
    page2 = [_member("3", True)]  # len < limit -> stop
    http = _FakeHttp([page1, page2])
    boosters = asyncio.run(
        nitro.fetch_booster_discord_ids(http, guild_id="G", page_limit=2, page_pause=0)
    )
    assert boosters == {"1", "3"}  # only boosters, non-booster "2" excluded
    # Second page requested with after = last member id from page 1.
    assert http.after_calls == [None, "2"]


def test_fetch_boosters_dedupes_and_stops_on_empty():
    # First page is full (len == limit) so it continues; second call returns [].
    http = _FakeHttp([[_member("9", True), _member("9", True)]])
    boosters = asyncio.run(
        nitro.fetch_booster_discord_ids(http, guild_id="G", page_limit=2, page_pause=0)
    )
    assert boosters == {"9"}


def test_fetch_boosters_empty_guild():
    http = _FakeHttp([[]])
    boosters = asyncio.run(
        nitro.fetch_booster_discord_ids(http, guild_id="G", page_limit=2, page_pause=0)
    )
    assert boosters == set()


# --------------------------------------------------------------------------- #
# process_nitro_boosts_enabled — case-insensitive gate (.env ships "True")
# --------------------------------------------------------------------------- #
def test_process_flag_case_insensitive(monkeypatch):
    monkeypatch.setenv("PROCESS_NITRO_BOOSTS", "True")
    assert nitro.process_nitro_boosts_enabled() is True
    monkeypatch.setenv("PROCESS_NITRO_BOOSTS", "true")
    assert nitro.process_nitro_boosts_enabled() is True
    monkeypatch.setenv("PROCESS_NITRO_BOOSTS", "TRUE")
    assert nitro.process_nitro_boosts_enabled() is True
    monkeypatch.setenv("PROCESS_NITRO_BOOSTS", "false")
    assert nitro.process_nitro_boosts_enabled() is False
    monkeypatch.delenv("PROCESS_NITRO_BOOSTS", raising=False)
    assert nitro.process_nitro_boosts_enabled() is False


# --------------------------------------------------------------------------- #
# reconcile_nitro_legs — leg upsert/expire math (fake session + mocked model)
# --------------------------------------------------------------------------- #
def _leg(group_id, amount, status="active"):
    return SimpleNamespace(
        group_id=group_id,
        provider="nitro",
        status=status,
        tier_key=None,
        amount_cents=amount,
        current_period_end=None,
        cancel_at_period_end=False,
        user_id=None,
    )


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, existing):
        self._existing = existing
        self.added = []
        self.committed = False

    def query(self, *a, **k):
        return _FakeQuery(self._existing)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


def _patch_group_subscription():
    """Make db.models.GroupSubscription(**kw) return a fresh namespace, not the
    single shared MagicMock return_value (which would alias every new leg)."""
    import db.models as m

    m.GroupSubscription.side_effect = lambda group_id=None: SimpleNamespace(
        group_id=group_id,
        provider=None,
        status="none",
        tier_key=None,
        amount_cents=None,
        current_period_end=None,
        cancel_at_period_end=False,
        user_id=None,
    )


def test_reconcile_creates_updates_and_expires():
    _patch_group_subscription()
    existing = [_leg(10, 500), _leg(20, 1000)]  # group 20 loses all its boosters
    s = _FakeSession(existing)

    stats = nitro.reconcile_nitro_legs(s, {10: 3, 30: 1})

    # group 10: existing leg refreshed to 3 × $5.
    assert existing[0].amount_cents == 3 * nitro.NITRO_BOOST_CENTS
    assert existing[0].status == "active"
    assert existing[0].current_period_end is not None
    # group 20: no boosters now -> expired, zeroed.
    assert existing[1].status == "expired"
    assert existing[1].amount_cents == 0
    # group 30: brand-new leg.
    assert len(s.added) == 1
    new = s.added[0]
    assert new.group_id == 30
    assert new.amount_cents == 1 * nitro.NITRO_BOOST_CENTS
    assert new.provider == "nitro" and new.status == "active" and new.user_id is None
    assert s.committed is True
    assert stats == {
        "groups_credited": 2,
        "boosters_credited": 4,
        "legs_expired": 1,
    }


def test_reconcile_is_idempotent():
    _patch_group_subscription()
    existing = [_leg(10, 3 * nitro.NITRO_BOOST_CENTS)]
    s = _FakeSession(existing)

    nitro.reconcile_nitro_legs(s, {10: 3})

    assert existing[0].amount_cents == 3 * nitro.NITRO_BOOST_CENTS
    assert existing[0].status == "active"
    assert s.added == []  # no new legs on a no-op reconcile


def test_reconcile_skips_zero_counts():
    _patch_group_subscription()
    s = _FakeSession([])
    stats = nitro.reconcile_nitro_legs(s, {10: 0})
    assert s.added == []
    assert stats["groups_credited"] == 0 and stats["boosters_credited"] == 0


# --------------------------------------------------------------------------- #
# Booster messaging helpers (pure text)
# --------------------------------------------------------------------------- #
def test_format_cents():
    assert nitro.format_cents(500) == "$5"
    assert nitro.format_cents(250) == "$2.50"
    assert nitro.format_cents(1000) == "$10"
    assert nitro.format_cents(0) == "$0"


def test_dm_text_unlinked():
    txt = nitro.nitro_boost_dm_text({"linked": False}, per_boost_cents=500)
    assert "Link your Discord" in txt and "$5/mo" in txt


def test_dm_text_linked_no_group():
    txt = nitro.nitro_boost_dm_text({"linked": True, "groups": []}, per_boost_cents=500)
    assert "join a clan" in txt.lower()


def test_dm_text_single_group():
    ctx = {"linked": True, "groups": [{"id": 7, "name": "Kittens"}], "picked_group_name": "Kittens"}
    txt = nitro.nitro_boost_dm_text(ctx, per_boost_cents=500)
    assert "Kittens" in txt and "now supports" in txt
    assert "Pick a different clan" not in txt


def test_dm_text_multi_group_offers_picker():
    ctx = {
        "linked": True,
        "groups": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Bravo"}],
        "picked_group_name": "Bravo",
    }
    txt = nitro.nitro_boost_dm_text(ctx, per_boost_cents=500)
    assert "Bravo" in txt and "Pick a different clan" in txt


def test_summary_blocks_basic():
    entries = [
        {"discord_id": "1", "group": "Alpha"},
        {"discord_id": "2", "group": None},
    ]
    blocks = nitro.nitro_boost_summary_blocks(entries, 1000)
    assert len(blocks) == 1
    body = blocks[0]
    assert "$10/mo" in body  # 1000 cents credited
    assert "<@1> → **Alpha**" in body
    assert "<@2>" in body and "<@2> →" not in body  # groupless: no arrow


def test_summary_blocks_empty():
    assert nitro.nitro_boost_summary_blocks([], 0) == []


def test_summary_blocks_chunks_and_caps():
    entries = [{"discord_id": str(i), "group": "G" * 40} for i in range(400)]
    blocks = nitro.nitro_boost_summary_blocks(entries, 400 * 500)
    assert 1 < len(blocks) <= 10  # chunked, but capped at Discord's 10-embed limit


def test_announcement_text_with_and_without_group():
    with_group = nitro.nitro_boost_announcement_text(
        "<@42>", {"linked": True, "picked_group_name": "Alpha"}, per_boost_cents=500
    )
    assert "<@42>" in with_group and "Alpha" in with_group and "supports" in with_group

    without = nitro.nitro_boost_announcement_text("<@42>", {"linked": False}, per_boost_cents=500)
    assert "<@42>" in without and "thank you for the support" in without.lower()
    assert "supports" not in without
