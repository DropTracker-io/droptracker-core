"""Unit tests for data/submissions/clan_broadcast.py helper logic.

Follows the house pattern (see test_drop.py): the DB/services layers are
conftest-stubbed, so these tests exercise the pure/near-pure decision helpers
— guid determinism, clan-name binding, notification gating — not the full
session-bound processor flow (that's integration territory).
"""
from datetime import datetime

import pytest

from data.submissions.clan_broadcast import (
    GUID_BUCKET_SECONDS,
    _bound_group_ids,
    _clan_slug,
    _deterministic_guid,
    _notification_gate,
)


# ── deterministic guid ──────────────────────────────────────────────────────

def test_guid_is_deterministic_across_relayers():
    stamp = datetime(2026, 8, 5, 12, 0, 30)
    a = _deterministic_guid("my clan", "Alice received a drop: Twisted bow.", stamp)
    b = _deterministic_guid("my clan", "Alice received a drop: Twisted bow.", stamp)
    assert a == b
    assert a.startswith("cc:")
    assert len(a) == 3 + 64  # fits Drop.unique_id String(255)


def test_guid_same_bucket_same_guid():
    base = datetime(2026, 8, 5, 12, 0, 0)
    later = datetime.fromtimestamp(base.timestamp() + GUID_BUCKET_SECONDS - 1)
    assert _deterministic_guid("c", "m", base) == _deterministic_guid("c", "m", later)


def test_guid_differs_by_clan_message_and_bucket():
    stamp = datetime(2026, 8, 5, 12, 0, 0)
    next_bucket = datetime.fromtimestamp(stamp.timestamp() + GUID_BUCKET_SECONDS)
    base = _deterministic_guid("clan a", "msg", stamp)
    assert _deterministic_guid("clan b", "msg", stamp) != base
    assert _deterministic_guid("clan a", "other msg", stamp) != base
    assert _deterministic_guid("clan a", "msg", next_bucket) != base


# ── clan name normalization ─────────────────────────────────────────────────

def test_clan_slug_folds_case_spacing_and_markup():
    assert _clan_slug("The Best Clan") == _clan_slug("the best clan")
    assert _clan_slug("The Best Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("The_Best_Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("<img=3>The Best Clan") == _clan_slug("The Best Clan")
    assert _clan_slug("") == ""
    assert _clan_slug(None) == ""


# ── notification gate (mirrors drop_processor criteria) ─────────────────────

def _values(gid, min_value=None, send_stacks=None):
    values = {}
    if min_value is not None:
        values[(gid, "minimum_value_to_notify")] = min_value
    if send_stacks is not None:
        values[(gid, "send_stacks_of_items")] = send_stacks
    return values


def test_gate_unit_value_meets_minimum():
    assert _notification_gate(5, _values(5, "1000000"), 1_500_000, 1_500_000)
    assert not _notification_gate(5, _values(5, "2000000"), 1_500_000, 1_500_000)


def test_gate_stack_total_requires_send_stacks():
    # 200 x 50k = 10M stack: only announces when send_stacks is on.
    assert not _notification_gate(5, _values(5, "1000000"), 50_000, 10_000_000)
    assert _notification_gate(5, _values(5, "1000000", "1"), 50_000, 10_000_000)


def test_gate_defaults_to_2500000_on_missing_or_garbage_min():
    assert not _notification_gate(5, {}, 2_499_999, 2_499_999)
    assert _notification_gate(5, {}, 2_500_000, 2_500_000)
    assert _notification_gate(5, _values(5, "not-a-number"), 2_500_000, 2_500_000)


# ── group binding ───────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, group_ids):
        self._group_ids = group_ids

    def execute(self, *_args, **_kwargs):
        return _FakeResult([(gid,) for gid in self._group_ids])


class _FakePlayer:
    player_id = 42


def test_bound_groups_require_tracking_flag_and_clan_name_match(monkeypatch):
    from utils import group_config as gc

    config = {
        (10, "clan_broadcast_tracking"): "1",
        (10, "clan_chat_name"): "The Best Clan",
        (11, "clan_broadcast_tracking"): "1",
        (11, "clan_chat_name"): "Some Other Clan",
        (12, "clan_broadcast_tracking"): "0",   # opted out
        (12, "clan_chat_name"): "The Best Clan",
        (13, "clan_broadcast_tracking"): "1",   # opted in, name never set
        (10, "clan_broadcast_min_value"): "500000",
    }
    monkeypatch.setattr(gc, "get_bulk", lambda _s, _g, _k: config)

    bound = _bound_group_ids(
        _FakeSession([10, 11, 12, 13]), _FakePlayer(), _clan_slug("the_best_clan")
    )
    assert bound == {10: 500_000}


def test_bound_groups_skip_system_groups_and_handle_no_candidates(monkeypatch):
    from utils import group_config as gc

    monkeypatch.setattr(
        gc, "get_bulk",
        lambda _s, _g, _k: {
            (2, "clan_broadcast_tracking"): "1",
            (2, "clan_chat_name"): "Global",
        },
    )
    # Global group (2) and template (1) are never bindable.
    assert _bound_group_ids(_FakeSession([1, 2]), _FakePlayer(), _clan_slug("Global")) == {}
    assert _bound_group_ids(_FakeSession([]), _FakePlayer(), _clan_slug("x")) == {}


def test_bound_groups_bad_floor_defaults_to_zero(monkeypatch):
    from utils import group_config as gc

    monkeypatch.setattr(
        gc, "get_bulk",
        lambda _s, _g, _k: {
            (10, "clan_broadcast_tracking"): "true",
            (10, "clan_chat_name"): "Clan",
            (10, "clan_broadcast_min_value"): "banana",
        },
    )
    assert _bound_group_ids(_FakeSession([10]), _FakePlayer(), _clan_slug("Clan")) == {10: 0}


# ── plugin-user reconciliation: defer + sweep ────────────────────────────────

import asyncio
import json

from data.submissions import clan_broadcast as cb


class _FakeRedis:
    """Just enough of a Redis client for the deferred ZSET round-trip."""

    def __init__(self):
        self.zsets = {}

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrangebyscore(self, key, _lo, hi, start=0, num=None):
        due = sorted(
            (m for m, s in self.zsets.get(key, {}).items() if s <= hi),
            key=lambda m: self.zsets[key][m],
        )
        return due[start: start + num if num else None]

    def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cb.redis_client, "client", fake, raising=False)
    return fake


def test_defer_parks_original_payload_with_grace(fake_redis, monkeypatch):
    monkeypatch.setattr(cb, "PLUGIN_GRACE_SECONDS", 300)
    payload = {"message": "X received a drop: Y", "_received_at": "2026-08-05 10:00:00"}
    assert cb._defer_broadcast(payload, "main") is True
    (member, score), = fake_redis.zsets[cb.DEFERRED_ZSET_KEY].items()
    entry = json.loads(member)
    assert entry["broadcast_data"] == payload
    assert entry["world_type"] == "main"
    # Due in the future — the sweep must not pick it up yet.
    assert cb.process_due_deferred_broadcasts.__name__  # module sanity
    assert score > __import__("time").time() + 200


def test_sweep_replays_only_due_entries_with_replay_flag(fake_redis, monkeypatch):
    monkeypatch.setattr(cb, "PLUGIN_GRACE_SECONDS", -1)  # due immediately
    cb._defer_broadcast({"message": "due"}, "main")
    monkeypatch.setattr(cb, "PLUGIN_GRACE_SECONDS", 3600)  # far future
    cb._defer_broadcast({"message": "not yet"}, "main")

    calls = []

    async def fake_processor(broadcast_data, external_session=None, world_type="main",
                             _deferred_replay=False):
        calls.append((broadcast_data, world_type, _deferred_replay))

    monkeypatch.setattr(cb, "clan_broadcast_processor", fake_processor)
    replayed = asyncio.run(cb.process_due_deferred_broadcasts())
    assert replayed == 1
    assert calls == [({"message": "due"}, "main", True)]
    # The undue entry is still parked.
    assert len(fake_redis.zsets[cb.DEFERRED_ZSET_KEY]) == 1


def test_sweep_retries_failed_replay_then_drops(fake_redis, monkeypatch):
    monkeypatch.setattr(cb, "PLUGIN_GRACE_SECONDS", -1)
    cb._defer_broadcast({"message": "boom"}, "main")

    async def exploding_processor(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cb, "clan_broadcast_processor", exploding_processor)

    for expected_attempts in (1, 2):
        assert asyncio.run(cb.process_due_deferred_broadcasts()) == 0
        entries = [json.loads(m) for m in fake_redis.zsets[cb.DEFERRED_ZSET_KEY]]
        assert [e["attempts"] for e in entries] == [expected_attempts]
        # Force the retry due now.
        key = cb.DEFERRED_ZSET_KEY
        fake_redis.zsets[key] = {m: 0 for m in fake_redis.zsets[key]}

    # Third failure exhausts DEFERRED_MAX_ATTEMPTS — entry is dropped.
    assert asyncio.run(cb.process_due_deferred_broadcasts()) == 0
    assert fake_redis.zsets[cb.DEFERRED_ZSET_KEY] == {}


def test_plugin_copy_check_matches_non_chat_drop_in_window(monkeypatch):
    """Drops: covered iff a non-clan_chat row for player+item exists in the
    look-back window. The query chain is faked; the assertion is on the
    decision, not the SQL."""
    from datetime import datetime

    class _Item:
        item_id = 999

    async def fake_ensure_item(_session, _name):
        return _Item()

    monkeypatch.setattr(cb, "ensure_item_by_name", fake_ensure_item)
    # db.models is conftest-stubbed (MagicMock columns don't support >=, and
    # the real sqlalchemy or_() rejects MagicMock operands). The SQL itself
    # isn't under test here — only the covered/missing decision.
    import sqlalchemy

    import db.models as dm

    monkeypatch.setattr(sqlalchemy, "or_", lambda *clauses: ("or", clauses))

    class _Col:
        def __eq__(self, other):
            return ("eq", other)

        def __ne__(self, other):
            return ("ne", other)

        def __ge__(self, other):
            return ("ge", other)

        def is_(self, other):
            return ("is", other)

    class _Drop:
        drop_id = _Col()
        player_id = _Col()
        item_id = _Col()
        date_added = _Col()
        source = _Col()

    monkeypatch.setattr(dm, "Drop", _Drop)

    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return self._row

    class _Session:
        def __init__(self, row):
            self._row = row

        def query(self, *_a, **_k):
            return _Query(self._row)

    class _Subject:
        player_id = 7

    parsed = cb.parse_broadcast("Alice received a drop: Twisted bow (1,000,000 coins)")
    stamp = datetime(2026, 8, 5, 12, 0, 0)
    covered = asyncio.run(cb._plugin_copy_exists(_Session(("row",)), _Subject(), parsed, stamp))
    missing = asyncio.run(cb._plugin_copy_exists(_Session(None), _Subject(), parsed, stamp))
    assert covered is True
    assert missing is False


def test_plugin_copy_check_unresolvable_item_is_not_covered(monkeypatch):
    from datetime import datetime

    async def fake_ensure_item(_session, _name):
        return None

    monkeypatch.setattr(cb, "ensure_item_by_name", fake_ensure_item)
    parsed = cb.parse_broadcast("Alice received a drop: Twisted bow")
    assert asyncio.run(
        cb._plugin_copy_exists(object(), object(), parsed, datetime(2026, 8, 5))
    ) is False


# ── personal bests from broadcasts ───────────────────────────────────────────

def test_pb_time_parsing_display_formats():
    assert cb._pb_time_to_ms("1:04") == 64_000
    assert cb._pb_time_to_ms("21:55.80") == 21 * 60_000 + 55_800
    assert cb._pb_time_to_ms("0:31.20") == 31_200
    assert cb._pb_time_to_ms("garbage") == 0
    assert cb._pb_time_to_ms(None) == 0


def test_pb_bracket_from_broadcast_line():
    raid = cb.parse_broadcast(
        "Raid Leader has achieved a new Chambers of Xeric (Team Size: 5) personal best: 21:55.80"
    )
    solo = cb.parse_broadcast("Speed Runner has achieved a new Vorkath personal best: 1:04.")
    assert cb._pb_bracket(raid) == "5"
    assert cb._pb_bracket(solo) == "Solo"


def test_pb_coverage_requires_equal_or_faster_stored_time(monkeypatch):
    """Plugin-user reconciliation for PBs: covered iff the stored
    (player, boss, bracket) row is already equal-or-faster. The query chain is
    faked; the decision — and that npc resolution failure means NOT covered —
    is what's under test."""
    from datetime import datetime

    monkeypatch.setattr(cb, "_resolve_pb_npc", lambda _s, _a: (14176, "Yama"))

    class _Col:
        def __eq__(self, other):
            return ("eq", other)

        def __le__(self, other):
            return ("le", other)

        def __gt__(self, other):
            return ("gt", other)

    class _PB:
        id = _Col()
        player_id = _Col()
        npc_id = _Col()
        team_size = _Col()
        personal_best = _Col()

    import db.models as dm

    monkeypatch.setattr(dm, "PersonalBestEntry", _PB)

    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *_a):
            return self

        def first(self):
            return self._row

    class _Session:
        def __init__(self, row):
            self._row = row

        def query(self, *_a):
            return _Query(self._row)

    class _Subject:
        player_id = 7

    parsed = cb.parse_broadcast("Speed Runner has achieved a new Vorkath personal best: 1:04.")
    stamp = datetime(2026, 8, 5, 12, 0, 0)
    assert asyncio.run(cb._plugin_copy_exists(_Session(("row",)), _Subject(), parsed, stamp)) is True
    assert asyncio.run(cb._plugin_copy_exists(_Session(None), _Subject(), parsed, stamp)) is False

    # Unresolvable boss → not covered (record path skips it independently).
    monkeypatch.setattr(cb, "_resolve_pb_npc", lambda _s, _a: (None, None))
    assert asyncio.run(cb._plugin_copy_exists(_Session(("row",)), _Subject(), parsed, stamp)) is False


# ── drop source attribution (resolve-only, sentinel fallback) ────────────────

def test_resolve_npc_matches_the_named_source():
    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *_a):
            return self

        def first(self):
            return self._row

    class _Session:
        def __init__(self, row):
            self._row = row

        def query(self, *_a):
            return _Query(self._row)

    assert cb._resolve_npc(_Session((8615, "Alchemical Hydra")), "Alchemical Hydra") == (
        8615, "Alchemical Hydra"
    )


def test_resolve_npc_without_a_source_never_queries():
    """Untradeables and source-less wordings fall through to the sentinel
    before any DB work — a None session would raise if they didn't."""
    assert cb._resolve_npc(None, None) == (None, None)
    assert cb._resolve_npc(None, "") == (None, None)


# ── images-only gate vs a source that can never have an image ───────────────

def _images_gate(monkeypatch, images_only, override=None):
    async def fake_required(_session, _gid):
        return images_only

    from utils import group_config as gc

    monkeypatch.setattr(cb, "screenshot_required", fake_required)
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    monkeypatch.setattr(
        gc, "get",
        lambda _s, _g, _k, default=None: default if override is None else override,
    )
    return asyncio.run(cb._images_gate_blocks(object(), 7))


def test_images_gate_is_moot_without_the_images_only_setting(monkeypatch):
    assert _images_gate(monkeypatch, images_only=False) is False


def test_images_gate_defaults_to_notifying_anyway(monkeypatch):
    """Key unset = notify: a relayed broadcast can never carry a screenshot, so
    honouring images-only here would silently void clan broadcast tracking."""
    assert _images_gate(monkeypatch, images_only=True) is False


def test_images_gate_blocks_only_when_the_group_opts_out(monkeypatch):
    assert _images_gate(monkeypatch, images_only=True, override="0") is True
    assert _images_gate(monkeypatch, images_only=True, override="1") is False


# ── chat-bridge mirror: independent of every tracking decision ───────────────

class _MirrorSpy:
    """Stands in for _mirror_to_bridge, recording what it was handed."""

    def __init__(self, staged=1):
        self.calls = []
        self.staged = staged

    def __call__(self, _session, _relayer, clan_slug, message, _parsed, deferred_replay):
        self.calls.append((clan_slug, message, deferred_replay))
        return self.staged


@pytest.fixture
def mirror_spy(monkeypatch):
    """Processor stubbed down to the mirror decision — session selection,
    relayer auth, rate limiting and the ops counters aren't under test here."""
    spy = _MirrorSpy()

    async def fake_auth(_session, _name, _hash, _key):
        return _FakePlayer(), True, True

    monkeypatch.setattr(cb, "select_session_and_flag", lambda _ext: (object(), False))
    monkeypatch.setattr(cb, "ensure_player_and_auth", fake_auth)
    monkeypatch.setattr(cb, "_mirror_to_bridge", spy)
    monkeypatch.setattr(cb, "_relayer_within_rate_limit", lambda _pid: True)
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "_log_unknown_broadcast", lambda _t: None)
    return spy


def _payload(message):
    return {
        "message": message,
        "clan_name": "The Best Clan",
        "player_name": "Relayer",
        "acc_hash": "12345",
    }


def test_unrecognized_broadcast_is_still_mirrored(mirror_spy):
    line = "Some brand new Jagex wording nobody has a pattern for!"
    response = asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    assert mirror_spy.calls == [(cb._clan_slug("The Best Clan"), line, False)]
    assert response.success is True
    assert "mirrored" in response.message


def test_untracked_kind_is_still_mirrored(mirror_spy):
    line = "Quester has completed a quest: Dragon Slayer II."
    response = asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    assert [call[1] for call in mirror_spy.calls] == [line]
    assert response.success is True
    assert "not tracked" in response.message
    assert "mirrored" in response.message


def test_mirror_happens_when_no_group_tracks_broadcasts(mirror_spy, monkeypatch):
    """Bridge-only groups: the tracking binding is empty, chat still syncs."""
    monkeypatch.setattr(cb, "_bound_group_ids", lambda *_a: {})
    line = "Alice received a drop: Twisted bow (1,000,000 coins)."
    response = asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    assert [call[1] for call in mirror_spy.calls] == [line]
    assert response.success is True
    assert "clan broadcast tracking" in response.message


def test_unauthenticated_relayer_never_reaches_the_mirror(mirror_spy, monkeypatch):
    async def fake_auth(*_a):
        return None, False, False

    monkeypatch.setattr(cb, "ensure_player_and_auth", fake_auth)
    response = asyncio.run(cb.clan_broadcast_processor(_payload("Anything at all")))
    assert mirror_spy.calls == []
    assert response.success is False


def _fake_bridge_module(monkeypatch, allowed=True, staged=2, boom=False):
    import sys
    import types

    module = types.ModuleType("services.clan_chat_bridge")

    def mirror(_session, _pid, _slug, _message):
        if boom:
            raise RuntimeError("redis down")
        return staged

    module.mirror_broadcast_line = mirror
    module.relayer_within_rate_limit = lambda _pid: allowed
    monkeypatch.setitem(sys.modules, "services.clan_chat_bridge", module)


def _mirror(parsed=None, deferred=False):
    return cb._mirror_to_bridge(object(), _FakePlayer(), "clan", "a line", parsed, deferred)


def test_mirror_to_bridge_stages_and_counts(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, staged=2)
    assert _mirror() == 2


def test_mirror_to_bridge_skips_deferred_replays(monkeypatch):
    _fake_bridge_module(monkeypatch, staged=2)
    # The first pass mirrored this line minutes ago; the replay must not repeat it.
    assert _mirror(deferred=True) == 0


def test_mirror_to_bridge_respects_the_bridge_rate_limit(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, allowed=False)
    assert _mirror() == 0


def test_mirror_failure_never_costs_the_tracking_record(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, boom=True)
    assert _mirror() == 0


def test_channel_presence_churn_is_never_mirrored(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, staged=2)
    assert _mirror(parsed=cb.parse_broadcast("Logger On has joined.")) == 0
    assert _mirror(parsed=cb.parse_broadcast("Logger Off has left.")) == 0
    # Membership churn is rare and meaningful — it still syncs.
    assert _mirror(parsed=cb.parse_broadcast("Quitter has left the clan.")) == 2


def test_unparsed_lines_still_mirror_so_a_rewording_cannot_mute_the_bridge(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, staged=2)
    assert cb.parse_broadcast("Brand new Jagex wording") is None
    assert _mirror(parsed=None) == 2
