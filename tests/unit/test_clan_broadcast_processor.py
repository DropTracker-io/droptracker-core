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
    _notify_enabled_groups,
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
        item_name = "Twisted bow"

    async def fake_ensure_item(_session, _name):
        return _Item()

    monkeypatch.setattr(cb, "ensure_item_by_name", fake_ensure_item)
    monkeypatch.setattr(cb, "_equivalent_item_ids", lambda _s, item: [item.item_id])
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

        def in_(self, other):
            return ("in", other)

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


def test_equivalent_item_ids_folds_every_row_sharing_the_name(monkeypatch):
    """Ticket #491: the plugin stored noted Runite ore (452), the name lookup
    picked 451. Coverage must compare against both."""
    import db.models as dm

    seen = {}

    class _Col:
        def __eq__(self, other):
            seen["name"] = other
            return ("eq", other)

    class _ItemList:
        item_id = "item_id"
        item_name = _Col()

    monkeypatch.setattr(dm, "ItemList", _ItemList, raising=False)

    class _Query:
        def filter(self, *_a):
            return self

        def all(self):
            return [(451,), (452,), (None,)]

    class _Session:
        def query(self, *_a):
            return _Query()

    class _Item:
        item_id = 451
        item_name = "Runite ore"

    assert cb._equivalent_item_ids(_Session(), _Item()) == [451, 452]
    assert seen["name"] == "Runite ore"


def test_equivalent_item_ids_falls_back_to_the_exact_id_on_error():
    class _Boom:
        def query(self, *_a):
            raise RuntimeError("db down")

    class _Item:
        item_id = 8465
        item_name = "Dragon bolts (unf)"

    assert cb._equivalent_item_ids(_Boom(), _Item()) == [8465]


def test_plugin_copy_check_queries_the_whole_name_family(monkeypatch):
    """The drop query filters on item_id IN (family), not == the looked-up id."""
    from datetime import datetime

    import sqlalchemy

    import db.models as dm

    class _Item:
        item_id = 451
        item_name = "Runite ore"

    async def fake_ensure_item(_session, _name):
        return _Item()

    monkeypatch.setattr(cb, "ensure_item_by_name", fake_ensure_item)
    monkeypatch.setattr(cb, "_equivalent_item_ids", lambda _s, _i: [451, 452])
    monkeypatch.setattr(sqlalchemy, "or_", lambda *clauses: ("or", clauses))

    class _Col:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return (self.name, "eq", other)

        def __ne__(self, other):
            return (self.name, "ne", other)

        def __ge__(self, other):
            return (self.name, "ge", other)

        def is_(self, other):
            return (self.name, "is", other)

        def in_(self, other):
            return (self.name, "in", tuple(other))

    class _Drop:
        drop_id = _Col("drop_id")
        player_id = _Col("player_id")
        item_id = _Col("item_id")
        date_added = _Col("date_added")
        source = _Col("source")

    monkeypatch.setattr(dm, "Drop", _Drop)
    filters = []

    class _Query:
        def filter(self, *clauses):
            filters.extend(clauses)
            return self

        def first(self):
            return ("row",)

    class _Session:
        def query(self, *_a):
            return _Query()

    class _Subject:
        player_id = 5772293

    parsed = cb.parse_broadcast("Akizoku received a drop: 27 x Runite ore (270,405 coins) from The Leviathan.")
    assert asyncio.run(cb._plugin_copy_exists(_Session(), _Subject(), parsed, datetime(2026, 10, 19, 12, 41, 37))) is True
    assert ("item_id", "in", (451, 452)) in filters
    assert not any(isinstance(f, tuple) and f[:2] == ("item_id", "eq") for f in filters)


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
    # Precise times are already on the game's 600ms tick grid and pass through
    # untouched; a whole second means the broadcasting client had precise
    # timing off, so it snaps up to the slowest tick that display could mean
    # (utils.pb_time) rather than being credited with a time it may not have
    # achieved.
    assert cb._pb_time_to_ms("1:04") == 64_200
    # 1:05 is the discriminating case: 65_400 is the slowest tick a display of
    # 65s can stand for, while the old nearest-tick rule picked 64_800 — a time
    # the broadcaster may never have achieved.
    assert cb._pb_time_to_ms("1:05") == 65_400
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


# ── per-kind notify toggles: tracking is not a re-opt-in to announcements ───

def _enabled(monkeypatch, config, key, gids=(10, 11, 12)):
    from utils import group_config as gc

    monkeypatch.setattr(gc, "get_bulk", lambda _s, _g, _k: config)
    return _notify_enabled_groups(object(), gids, key)


def test_notify_toggle_off_excludes_the_group(monkeypatch):
    """Group 144's live bug: notify_clogs=0 still got chat-relayed clog embeds."""
    config = {(10, "notify_clogs"): "1", (11, "notify_clogs"): "0"}
    assert _enabled(monkeypatch, config, "notify_clogs", gids=(10, 11)) == {10}


def test_notify_toggle_unset_reads_as_registry_default(monkeypatch):
    """Matches gc.get_effective in clog_processor/pet_processor (ticket 495):
    a group with no row gets what the site shows, which is on for clogs."""
    assert _enabled(monkeypatch, {}, "notify_clogs", gids=(10,)) == {10}


def test_notify_toggle_accepts_both_truthy_spellings(monkeypatch):
    config = {
        (10, "notify_pets"): "true",
        (11, "notify_pets"): "1",
        (12, "notify_pets"): "false",
    }
    assert _enabled(monkeypatch, config, "notify_pets") == {10, 11}


def test_notify_toggle_dedupes_repeated_group_ids(monkeypatch):
    config = {(10, "notify_pbs"): "1"}
    assert _enabled(monkeypatch, config, "notify_pbs", gids=(10, 10, 10)) == {10}


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

    def mirror(_session, _pid, _slug, _message, parsed=None):
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


def test_client_channel_notices_are_never_mirrored(monkeypatch):
    """The login hint is addressed to the relayer, not the clan: one line per
    member per login, and nothing a Discord reader can act on."""
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, staged=2)
    notice = "To talk in your clan's channel, start each line of chat with // or /c."
    assert cb.parse_broadcast(notice).kind in cb.MIRROR_SUPPRESSED_KINDS
    assert _mirror(parsed=cb.parse_broadcast(notice)) == 0
    assert _mirror(parsed=cb.parse_broadcast("Attempting to join clan channel...")) == 0


def test_unparsed_lines_still_mirror_so_a_rewording_cannot_mute_the_bridge(monkeypatch):
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    _fake_bridge_module(monkeypatch, staged=2)
    assert cb.parse_broadcast("Brand new Jagex wording") is None
    assert _mirror(parsed=None) == 2


# ── bridge-only relays (plugin 6.0.17+) ─────────────────────────────────────

def test_bridge_only_relay_mirrors_but_never_reaches_tracking(mirror_spy, monkeypatch):
    """A relayer with only the chat bridge on forwards broadcasts so the
    channel shows them (the missing-Twisted-bow bug), but never opted into
    feeding records: the line stops right after the mirror."""
    def tracking_must_not_run(*_a, **_k):
        raise AssertionError("bridge-only line reached tracking")

    monkeypatch.setattr(cb, "_bound_group_ids", tracking_must_not_run)
    line = "Loggy received special loot from a raid: Twisted bow."
    payload = dict(_payload(line), bridge_only="true")
    response = asyncio.run(cb.clan_broadcast_processor(payload))
    assert [call[1] for call in mirror_spy.calls] == [line]
    assert response.success is True
    assert "bridge only" in response.message


def test_flag_absent_or_false_keeps_the_tracking_path():
    assert cb._is_bridge_only({}) is False
    assert cb._is_bridge_only({"bridge_only": "false"}) is False
    assert cb._is_bridge_only({"bridge_only": "true"}) is True
    assert cb._is_bridge_only({"bridge_only": True}) is True


# ── team lines + delve (2026-10-15) ─────────────────────────────────────────

class _Subject:
    def __init__(self, pid, name, plugin=False):
        self.player_id = pid
        self.player_name = name
        self.account_hash = "998877" if plugin else None


@pytest.fixture
def team_flow(mirror_spy, monkeypatch):
    """Processor stubbed past binding/dedupe; records which subjects reached
    the PB / delve writers and which lines were deferred."""
    roster = {
        "30 th": _Subject(1, "30 th"),
        "mish mish": _Subject(2, "mish mish", plugin=True),
        "Spoon Feed": _Subject(3, "Spoon Feed"),
    }
    calls = {"pb": [], "delve": [], "defer": 0, "guids": []}

    monkeypatch.setattr(cb, "_bound_group_ids", lambda *_a: {10: 0})
    monkeypatch.setattr(cb, "_bundle_is_new", lambda *_a: True)
    monkeypatch.setattr(cb, "received_at", lambda _d: datetime(2026, 10, 15, 12, 0))
    monkeypatch.setattr(
        cb, "_find_subject_in_groups",
        lambda _s, name, gids: (roster.get(name), [10]) if name in roster else (None, []),
    )

    async def no_copy(*_a):
        return False

    def defer(*_a):
        calls["defer"] += 1
        return True

    async def pb(_s, _ext, _parsed, subject, _gids, guid, _stamp):
        calls["pb"].append(subject.player_name)
        calls["guids"].append(guid)
        return cb.SubmissionResponse(True, "ok")

    def delve(_s, _ext, parsed, subject, _stamp):
        calls["delve"].append((subject.player_name, parsed.extra["level"]))
        return cb.SubmissionResponse(True, "ok")

    monkeypatch.setattr(cb, "_plugin_copy_exists", no_copy)
    monkeypatch.setattr(cb, "_defer_broadcast", defer)
    monkeypatch.setattr(cb, "_process_chat_pb", pb)
    monkeypatch.setattr(cb, "_process_chat_delve", delve)
    return calls


TOA_TEAM_LINE = (
    "30 th, Nobody Here and Spoon Feed achieved a new Tombs of Amascut (team size: 3) "
    "Expert mode Challenge personal best: 23:20"
)


def test_team_pb_credits_every_rostered_teammate(team_flow):
    response = asyncio.run(cb.clan_broadcast_processor(_payload(TOA_TEAM_LINE)))
    assert response.success
    assert team_flow["pb"] == ["30 th", "Spoon Feed"]
    assert len(set(team_flow["guids"])) == 2


def test_team_pb_defers_plugin_users_once_and_records_the_rest(team_flow):
    line = TOA_TEAM_LINE.replace("Nobody Here", "mish mish")
    asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    assert team_flow["pb"] == ["30 th", "Spoon Feed"]
    assert team_flow["defer"] == 1
    # The replay decides the plugin user; the others hit their guids again.
    asyncio.run(cb.clan_broadcast_processor(_payload(line), _deferred_replay=True))
    assert team_flow["pb"][2:] == ["30 th", "mish mish", "Spoon Feed"]
    assert team_flow["defer"] == 1


def test_single_player_guid_is_unchanged_by_the_team_support(team_flow):
    line = TOA_TEAM_LINE.replace("30 th, Nobody Here and Spoon Feed", "Spoon Feed")
    asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    stamp = datetime(2026, 10, 15, 12, 0)
    assert team_flow["guids"] == [
        cb._deterministic_guid(cb._clan_slug("The Best Clan"), line, stamp)
    ]


def test_delve_skips_plugin_reconciliation(team_flow):
    line = "mish mish achieved a new personal best deep delve at the Doom of Mokhaiotl: Level 31"
    asyncio.run(cb.clan_broadcast_processor(_payload(line)))
    assert team_flow["delve"] == [("mish mish", 31)]
    assert team_flow["defer"] == 0


# ── chat clog notifications name a source (ticket #476) ─────────────────────

def _run_chat_clog(monkeypatch, inferred):
    import asyncio
    from types import SimpleNamespace

    sent = []

    async def _item(_s, _name):
        return SimpleNamespace(item_id=11920)

    async def _notify(kind, player_id, data, gid, existing_session=None):
        sent.append(data)

    async def _no_gate(_s, _gid):
        return False

    monkeypatch.setattr(cb.redis_client.client, "set", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(cb, "ensure_item_by_name", _item)
    monkeypatch.setattr(cb, "_notify_enabled_groups", lambda _s, gids, _k: set(gids))
    monkeypatch.setattr(cb, "_images_gate_blocks", _no_gate)
    monkeypatch.setattr(cb, "create_notification", _notify)
    monkeypatch.setattr(cb, "_inferred_drop_source", lambda _name, from_raid=False: inferred)
    parsed = SimpleNamespace(item_name="Dragon pickaxe", extra={"log_slots": 518, "log_total": 1717})
    subject = SimpleNamespace(player_name="Sebby", player_id=7)
    asyncio.run(cb._process_chat_clog(None, False, parsed, subject, [381], [381], "cc:x"))
    return sent


def test_chat_clog_uses_the_items_only_source(monkeypatch):
    sent = _run_chat_clog(monkeypatch, "Tormented Demon")
    assert [d["npc_name"] for d in sent] == ["Tormented Demon"]
    assert sent[0]["log_slots"] == 518


def test_chat_clog_without_a_sole_source_says_clan_broadcast(monkeypatch):
    sent = _run_chat_clog(monkeypatch, None)
    assert [d["npc_name"] for d in sent] == [cb.SENTINEL_NPC_NAME] == ["Clan Broadcast"]


# ── clan bank coffer sync (web144a) ─────────────────────────────────────────

import sys
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

import services.group_bank as gbs


class _Col:
    """A column stand-in whose comparisons the scripted query can read."""

    def __eq__(self, other):
        return ("eq", other)

    def in_(self, values):
        return ("in", tuple(values))

    __hash__ = object.__hash__


class _CofferRow:
    id = None
    group_id = _Col()
    external_ref = _Col()

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _CofferAudit:
    id = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _CofferQuery:
    def __init__(self, session):
        self.session = session
        self.args = ()

    def filter(self, *args):
        self.args = args
        return self

    def first(self):
        gid = next(a[1] for a in self.args if a[0] == "eq")
        refs = next(a[1] for a in self.args if a[0] == "in")
        self.session.lookups.append((gid, refs))
        return (1,) if any((gid, r) in self.session.booked for r in refs) else None


class _CofferSession:
    """Scripted session: ``booked`` holds (group_id, external_ref) pairs that
    already exist; ``race_groups`` makes the insert for a group hit the unique
    index, as when another relayer's copy wins between lookup and insert."""

    def __init__(self, booked=(), race_groups=(), fail_commit=False):
        self.booked = set(booked)
        self.race_groups = set(race_groups)
        self.fail_commit = fail_commit
        self.added = []
        self.lookups = []
        self.committed = False

    def query(self, *_a):
        return _CofferQuery(self)

    @contextmanager
    def begin_nested(self):
        mark = len(self.added)
        try:
            yield
        except Exception:
            del self.added[mark:]
            raise

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for i, obj in enumerate(self.added):
            if obj.id is None:
                obj.id = 500 + i
            if isinstance(obj, _CofferRow) and obj.group_id in self.race_groups:
                raise IntegrityError("INSERT", {}, Exception("Duplicate entry"))

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("db went away")
        self.committed = True

    def rollback(self):
        pass

    @property
    def rows(self):
        return [o for o in self.added if isinstance(o, _CofferRow)]

    @property
    def audits(self):
        return [o for o in self.added if isinstance(o, _CofferAudit)]


_STAMP = datetime(2026, 10, 23, 12, 0, 30)
_DEPOSIT = "Generous One has deposited 5,000,000 coins into the coffer."
_WITHDRAWAL = "Treasurer has withdrawn 2,000,000 coins from the coffer."
_SLUG = "the best clan"


@pytest.fixture
def coffer(monkeypatch):
    db_stub = sys.modules["db"]
    monkeypatch.setattr(db_stub, "GroupBankEntry", _CofferRow, raising=False)
    monkeypatch.setattr(db_stub, "AuditLog", _CofferAudit, raising=False)
    state = {"groups": {10: "pending"}, "occurrence": 1, "new_guid": True, "forgotten": []}
    monkeypatch.setattr(cb, "_coffer_group_ids", lambda _s, _r, _slug: state["groups"])
    monkeypatch.setattr(cb, "_coffer_occurrence", lambda _slug, _m, _pid: state["occurrence"])
    monkeypatch.setattr(cb, "_coffer_payload_is_new", lambda _g: state["new_guid"])
    monkeypatch.setattr(cb, "_forget_coffer_payload", lambda g: state["forgotten"].append(g))
    monkeypatch.setattr(cb, "_coffer_counterparty", lambda _s, name: (None, name))
    monkeypatch.setattr(cb, "_relayer_within_rate_limit", lambda _pid: True)
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "received_at", lambda _data: _STAMP)
    return state


def _run_coffer(session, line=_DEPOSIT, guid="g1"):
    from utils.clan_broadcasts import parse_broadcast

    return cb._process_coffer(
        session, False, {"guid": guid}, _FakePlayer(), _SLUG, line, parse_broadcast(line)
    )


def _ref(line=_DEPOSIT, occurrence=1, stamp=_STAMP):
    return gbs.coffer_refs(_SLUG, line, stamp.timestamp(), occurrence)[0]


def test_coffer_deposit_books_a_pending_donation(coffer):
    s = _CofferSession()
    response = _run_coffer(s)
    assert response.success is True and "booked for 1 group" in response.message
    (row,) = s.rows
    assert (row.group_id, row.kind, row.amount, row.status, row.source) == (
        10, "donation", 5_000_000, "pending", "coffer",
    )
    assert row.rsn == "Generous One" and row.player_id is None
    assert row.external_ref == _ref() and row.confirmed_at is None
    assert row.created_at == _STAMP
    (audit,) = s.audits
    assert audit.action == "group.bank.coffer" and audit.actor_user_id is None
    assert json.loads(audit.after)["relayed_by"] == _FakePlayer.player_id
    assert s.committed


def test_coffer_withdrawal_is_negative_and_auto_confirm_stamps_it(coffer):
    coffer["groups"] = {10: "confirmed"}
    s = _CofferSession()
    _run_coffer(s, _WITHDRAWAL)
    (row,) = s.rows
    assert row.kind == "withdrawal" and row.amount == -2_000_000
    assert row.status == "confirmed" and row.confirmed_at == _STAMP


def test_coffer_line_another_relayer_booked_is_skipped(coffer):
    s = _CofferSession(booked={(10, _ref())})
    response = _run_coffer(s)
    assert response.success is True and "already in the bank" in response.message
    assert s.rows == []


def test_coffer_relay_from_the_previous_bucket_is_matched(coffer):
    earlier = datetime.fromtimestamp(_STAMP.timestamp() - gbs.COFFER_BUCKET_SECONDS)
    s = _CofferSession(booked={(10, _ref(stamp=earlier))})
    _run_coffer(s)
    assert s.rows == []


def test_coffer_repeat_deposit_by_the_same_relayer_books_again(coffer):
    # Same line, but this relayer has now seen it twice: a second deposit.
    coffer["occurrence"] = 2
    s = _CofferSession(booked={(10, _ref(occurrence=1))})
    _run_coffer(s)
    (row,) = s.rows
    assert row.external_ref == _ref(occurrence=2)


def test_coffer_redelivered_payload_is_a_no_op(coffer):
    coffer["new_guid"] = False
    s = _CofferSession()
    response = _run_coffer(s)
    assert response.success is True and s.rows == [] and s.lookups == []


def test_coffer_insert_race_is_absorbed_by_the_unique_index(coffer):
    s = _CofferSession(race_groups={10})
    response = _run_coffer(s)
    assert response.success is True and s.rows == []
    assert s.committed and coffer["forgotten"] == []


def test_coffer_books_once_per_syncing_group_with_its_own_status(coffer):
    coffer["groups"] = {10: "pending", 11: "confirmed"}
    s = _CofferSession(booked={(11, _ref())})
    _run_coffer(s)
    assert [(r.group_id, r.status) for r in s.rows] == [(10, "pending")]


def test_coffer_without_a_syncing_group_writes_nothing(coffer):
    coffer["groups"] = {}
    s = _CofferSession()
    response = _run_coffer(s)
    assert response.success is True and "syncs this clan's coffer" in response.message
    assert s.rows == [] and not s.committed


def test_coffer_failed_write_releases_the_guid(coffer):
    s = _CofferSession(fail_commit=True)
    response = _run_coffer(s)
    assert response.success is False
    assert coffer["forgotten"] == ["g1"]


def test_coffer_lines_take_the_bank_path_not_drop_tracking(mirror_spy, monkeypatch):
    calls = []

    def _fake_coffer(*args):
        calls.append(args[5])
        return cb.SubmissionResponse(True, "ok")

    def _no_tracking(*_a, **_k):
        raise AssertionError("coffer lines must not reach drop-tracking binding")

    monkeypatch.setattr(cb, "_process_coffer", _fake_coffer)
    monkeypatch.setattr(cb, "_bound_group_ids", _no_tracking)
    response = asyncio.run(cb.clan_broadcast_processor(_payload(_DEPOSIT)))
    assert response.success is True and calls == [_DEPOSIT]
    # The bridge mirror still sees the line, as for every other kind.
    assert [call[1] for call in mirror_spy.calls] == [_DEPOSIT]


def test_coffer_groups_need_the_sync_flag_and_a_matching_clan(monkeypatch):
    from utils import group_config as gc

    config = {
        (10, "bank_coffer_sync"): "1",
        (10, "clan_chat_name"): "The Best Clan",
        (10, "bank_coffer_status"): "confirmed",
        (11, "bank_coffer_sync"): "1",
        (11, "clan_chat_name"): "The_Best_Clan",   # status unset -> review
        (12, "bank_coffer_sync"): "0",
        (12, "clan_chat_name"): "The Best Clan",
        (13, "bank_coffer_sync"): "1",
        (13, "clan_chat_name"): "Some Other Clan",
        # Drop tracking alone does not sync the coffer.
        (14, "clan_broadcast_tracking"): "1",
        (14, "clan_chat_name"): "The Best Clan",
    }
    monkeypatch.setattr(gc, "get_bulk", lambda _s, _g, _k: config)
    bound = cb._coffer_group_ids(
        _FakeSession([10, 11, 12, 13, 14]), _FakePlayer(), _clan_slug("The Best Clan")
    )
    assert bound == {10: "confirmed", 11: "pending"}
