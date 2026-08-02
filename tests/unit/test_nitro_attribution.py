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
        "boosts_credited": 4,
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
    assert stats["groups_credited"] == 0 and stats["boosts_credited"] == 0


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


# --------------------------------------------------------------------------- #
# Multi-boost slot counting.
#
# Discord gives no per-member boost count. These cover the three signals that
# stand in for one: the boost system message, the admin override, and
# premium_subscription_count as the ceiling.
# --------------------------------------------------------------------------- #
def _boost_msg(author_id, content, msg_type=8, msg_id="1"):
    return {"id": msg_id, "type": msg_type, "content": content, "author": {"id": author_id}}


def test_boost_message_empty_content_is_one_boost():
    # Discord renders a first/only boost as "just boosted the server!" with no
    # count in `content` — the overwhelmingly common case.
    assert nitro.boost_count_from_message(_boost_msg("7", "")) == ("7", 1)
    assert nitro.boost_count_from_message(_boost_msg("7", None)) == ("7", 1)


def test_boost_message_content_carries_the_count():
    assert nitro.boost_count_from_message(_boost_msg("7", "2")) == ("7", 2)
    assert nitro.boost_count_from_message(_boost_msg("7", " 3 ")) == ("7", 3)


def test_boost_message_tier_types_also_count():
    # Types 9/10/11 are a boost that ALSO levelled the guild up; same payload.
    for msg_type in (9, 10, 11):
        assert nitro.boost_count_from_message(_boost_msg("7", "2", msg_type)) == ("7", 2)


def test_boost_message_ignores_non_boost_and_malformed():
    assert nitro.boost_count_from_message(_boost_msg("7", "2", msg_type=0)) is None
    assert nitro.boost_count_from_message({"type": 8, "content": "2"}) is None  # no author
    assert nitro.boost_count_from_message({}) is None
    # Junk content must not crash or inflate — fall back to one slot.
    assert nitro.boost_count_from_message(_boost_msg("7", "lots")) == ("7", 1)


def test_boost_message_count_is_capped():
    huge = nitro.boost_count_from_message(_boost_msg("7", "99999"))
    assert huge == ("7", nitro.MAX_BOOST_SLOTS_PER_USER)


def test_resolve_defaults_to_one_slot_each():
    counts, diag = nitro.resolve_boost_counts({"a", "b"})
    assert counts == {"a": 1, "b": 1}
    assert diag["boosters"] == 2 and diag["attributed"] == 2


def test_resolve_applies_observed_multi_boost():
    # The bug this whole path exists for: one member, two boosts.
    counts, diag = nitro.resolve_boost_counts(
        {"a", "b"}, observed={"a": 2}, guild_total=3
    )
    assert counts == {"a": 2, "b": 1}
    assert diag["attributed"] == 3 and diag["unattributed"] == 0


def test_resolve_ignores_signals_for_non_boosters():
    # Stale message/override for someone who stopped boosting must not credit.
    counts, _ = nitro.resolve_boost_counts(
        {"a"}, observed={"gone": 5}, overrides={"also_gone": 3}
    )
    assert counts == {"a": 1}


def test_resolve_override_beats_observed():
    counts, diag = nitro.resolve_boost_counts(
        {"a"}, observed={"a": 4}, overrides={"a": 1}, guild_total=1
    )
    assert counts == {"a": 1}  # admin corrected a stale message count down
    assert diag["manual_overrides"] == 1


def test_resolve_reports_unattributed_without_crediting():
    # 13 slots on the guild, 9 boosters, only one known multi-booster: the
    # remainder is surfaced, never invented onto a group.
    boosters = {str(i) for i in range(9)}
    counts, diag = nitro.resolve_boost_counts(boosters, observed={"0": 2}, guild_total=13)
    assert sum(counts.values()) == 10
    assert diag["unattributed"] == 3
    assert diag["guild_total"] == 13 and diag["boosters"] == 9


def test_resolve_trims_over_attribution_to_the_guild_total():
    # A stale message log claims more slots than the guild actually has.
    counts, diag = nitro.resolve_boost_counts(
        {"a", "b"}, observed={"a": 5}, guild_total=3
    )
    assert sum(counts.values()) == 3
    assert counts["b"] == 1 and counts["a"] == 2
    assert diag["trimmed"] == 3


def test_resolve_trims_automatic_before_manual():
    counts, _ = nitro.resolve_boost_counts(
        {"a", "b"}, observed={"a": 3}, overrides={"b": 3}, guild_total=4
    )
    assert counts["b"] == 3  # admin's number survives
    assert counts["a"] == 1  # the unverified claim absorbs the trim


def test_resolve_flags_more_boosters_than_slots():
    # Can't be fixed by trimming (nobody is above 1) — report it instead.
    counts, diag = nitro.resolve_boost_counts({"a", "b", "c"}, guild_total=2)
    assert counts == {"a": 1, "b": 1, "c": 1}
    assert diag["over_attributed"] == 1


def test_resolve_without_guild_total_skips_clamping():
    counts, diag = nitro.resolve_boost_counts({"a"}, observed={"a": 3})
    assert counts == {"a": 3}
    assert diag["guild_total"] is None and diag["unattributed"] == 0


# --------------------------------------------------------------------------- #
# attribute_boosters — slots (not headcount) fold into the group's leg
# --------------------------------------------------------------------------- #
class _UserQuerySession:
    def __init__(self, users):
        self._users = users

    def query(self, *a, **k):
        return _FakeQuery(self._users)


def test_attribute_boosters_sums_slots_per_group(monkeypatch):
    users = [
        SimpleNamespace(user_id=1, discord_id="a"),
        SimpleNamespace(user_id=2, discord_id="b"),
    ]
    monkeypatch.setattr(nitro, "pick_group_for_user", lambda s, uid: 10)
    # Two members in the same group, one of them double-boosting -> 3 slots.
    assert nitro.attribute_boosters(_UserQuerySession(users), {"a": 2, "b": 1}) == {10: 3}


def test_attribute_boosters_accepts_a_bare_id_set(monkeypatch):
    """Back-compat: a plain iterable of ids still means one slot each."""
    users = [SimpleNamespace(user_id=1, discord_id="a")]
    monkeypatch.setattr(nitro, "pick_group_for_user", lambda s, uid: 10)
    assert nitro.attribute_boosters(_UserQuerySession(users), {"a"}) == {10: 1}


# --------------------------------------------------------------------------- #
# Discord fetches for the guild total + message history
# --------------------------------------------------------------------------- #
class _GuildHttp:
    def __init__(self, guild):
        self._guild = guild

    async def get_guild(self, guild_id, *a, **k):
        if isinstance(self._guild, Exception):
            raise self._guild
        return self._guild


def test_fetch_guild_boost_state_reads_total_and_channel():
    state = asyncio.run(nitro.fetch_guild_boost_state(
        _GuildHttp({
            "premium_subscription_count": 13,
            "system_channel_id": "555",
            "system_channel_flags": 4,
        }),
        guild_id="G",
    ))
    assert state["total"] == 13
    assert state["system_channel_id"] == "555"
    assert state["boost_messages_enabled"] is True


def test_fetch_guild_boost_state_honours_suppress_flag():
    # 1 << 1 set -> Discord posts no boost messages, so that signal is gone.
    state = asyncio.run(nitro.fetch_guild_boost_state(
        _GuildHttp({
            "premium_subscription_count": 4,
            "system_channel_id": "555",
            "system_channel_flags": 2,
        }),
        guild_id="G",
    ))
    assert state["total"] == 4 and state["boost_messages_enabled"] is False


def test_fetch_guild_boost_state_degrades_on_error():
    # A failed fetch must not take the reconcile down — no total means no clamp.
    state = asyncio.run(nitro.fetch_guild_boost_state(_GuildHttp(RuntimeError("403")), guild_id="G"))
    assert state == {"total": None, "system_channel_id": None, "boost_messages_enabled": False}


class _HistoryHttp:
    def __init__(self, pages):
        self._pages = list(pages)
        self.before_calls = []

    async def get_channel_messages(self, channel_id, limit=100, before=None, **k):
        self.before_calls.append(before)
        return self._pages.pop(0) if self._pages else []


def test_fetch_boost_message_counts_newest_wins():
    # Walk is newest-first; a member's latest message holds their current count.
    http = _HistoryHttp([[
        _boost_msg("a", "3", msg_id="30"),
        _boost_msg("a", "2", msg_id="20"),
        _boost_msg("b", "", msg_id="10"),
    ]])
    counts = asyncio.run(
        nitro.fetch_boost_message_counts(http, "C", page_limit=100, page_pause=0)
    )
    assert counts == {"a": 3, "b": 1}


def test_fetch_boost_message_counts_paginates_and_ignores_chatter():
    page1 = [_boost_msg("a", "2", msg_id="9"), {"id": "8", "type": 0, "content": "hi"}]
    page2 = [_boost_msg("b", "", msg_id="7")]
    http = _HistoryHttp([page1, page2])
    counts = asyncio.run(
        nitro.fetch_boost_message_counts(http, "C", page_limit=2, page_pause=0)
    )
    assert counts == {"a": 2, "b": 1}
    assert http.before_calls == [None, "8"]  # continued from the last id on page 1


def test_fetch_boost_message_counts_survives_permission_error():
    class _Boom:
        async def get_channel_messages(self, *a, **k):
            raise RuntimeError("Missing Access")

    assert asyncio.run(nitro.fetch_boost_message_counts(_Boom(), "C", page_pause=0)) == {}


def test_fetch_boost_message_counts_without_channel():
    assert asyncio.run(nitro.fetch_boost_message_counts(None, "", page_pause=0)) == {}


# --------------------------------------------------------------------------- #
# Booster messaging reflects the number of slots placed, not a flat one-boost
# figure (a double booster contributes $10/mo, and should be told so).
# --------------------------------------------------------------------------- #
def test_dm_text_quotes_multi_boost_credit():
    ctx = {"linked": True, "groups": [{"id": 1, "name": "Alpha"}], "boost_slots": 2}
    assert "$10/mo" in nitro.nitro_boost_dm_text(ctx, per_boost_cents=500)


def test_dm_text_defaults_to_one_slot_when_absent():
    ctx = {"linked": True, "groups": [{"id": 1, "name": "Alpha"}]}
    assert "$5/mo" in nitro.nitro_boost_dm_text(ctx, per_boost_cents=500)


def test_announcement_text_calls_out_multi_boost():
    text = nitro.nitro_boost_announcement_text(
        "<@42>",
        {"linked": True, "picked_group_name": "Alpha", "boost_slots": 2},
        per_boost_cents=500,
    )
    assert "2×" in text and "$10/mo" in text and "Alpha" in text


def test_announcement_text_single_boost_has_no_multiplier():
    text = nitro.nitro_boost_announcement_text(
        "<@42>", {"linked": True, "picked_group_name": "Alpha"}, per_boost_cents=500
    )
    assert "×" not in text and "$5/mo" in text
