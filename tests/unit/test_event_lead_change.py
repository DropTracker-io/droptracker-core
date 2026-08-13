"""Lead-change announcements (dev-tracker t60).

The reported bug: "lead change announcements didn't trigger reliably — only the
first tile and the final lead change before the blackout". Cause: the leader
comparison sat around ONE score write (the task's own points) in the middle of
``apply_ledger_row``, while bingo line/blackout bonuses score AFTER that window
closes — so a tile that took the lead announced and a line bonus that took the
lead never did. Detection now snapshots the leader before the first score write
of an apply and compares once after the last one.

What is pinned here:

* a bonus-driven overtake announces (the regression), including from a
  ZERO-point task — the old gate (``if points and team_id is not None``) never
  even looked at the standings for one;
* a team that keeps / never takes the lead announces nothing;
* tie semantics: tying the top score is not taking the lead, breaking the tie
  is;
* the payload carries a per-hand-over discriminator, so the
  ``notification_queue`` unique index on (type, player, group, data) cannot
  swallow a genuine repeat lead change;
* a hand-over caused by somebody ELSE's score dropping (a revoke) announces the
  team that inherited the lead;
* a lead change with no leaderboard/announcements channel is logged instead of
  vanishing;
* the cross-lane claim: the apply lanes are concurrent and READ COMMITTED, so
  several of them see the same hand-over in their post-apply read — exactly one
  announces it, and the observers stay quiet instead of posting the right team
  under their own unrelated task (the review's must-fix #2);
* ``team_score`` leaves as an int — the plugin types it as Integer and one
  fractional value makes gson discard the whole drained notification batch.

The engine is loaded straight from the file (test_event_engine_scoring's
pattern) and driven against a model-dispatched fake session, so the branching
is exercised without SQLAlchemy. The claim's Redis handle is a fake with the
single command it issues; every path that can announce runs inside one, so no
test depends on the conftest's auto-created ``utils.redis`` mock.
"""

import importlib.util
import json
import logging
import os
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _inject_real(name, filename):
    """Load a real services module past the conftest ``services`` stub."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "services", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if "services" in sys.modules:
        setattr(sys.modules["services"], name.split(".")[-1], module)
    spec.loader.exec_module(module)
    return module


# apply_ledger_row reads the message config through this on every completion.
_inject_real("services.event_notifications", "event_notifications.py")

_spec = importlib.util.spec_from_file_location(
    "_event_lead_change_ut", os.path.join(_ROOT, "services", "event_engine.py"))
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_lead_change_ut"] = engine
_spec.loader.exec_module(engine)


# ── Fake ORM ─────────────────────────────────────────────────────────────────

class _Pred:
    """An ``==`` filter the fake session actually applies (team/event scoping
    decides which rows a query sees, and lead detection depends on it)."""

    def __init__(self, name, value):
        self.name, self.value = name, value


class _Col:
    """Column stand-in. ``==`` yields a predicate; every other expression form
    (in_/is_/isnot/desc/asc) yields something the fake ignores."""

    def __init__(self, model, name):
        self._model, self._name = model, name

    def __eq__(self, other):
        return _Pred(self._name, other)

    def __hash__(self):
        return id(self)

    def in_(self, *a):
        return None

    def is_(self, *a):
        return None

    def isnot(self, *a):
        return None

    def desc(self):
        return None

    def asc(self):
        return None


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getitem__(self, i):
        # Column queries return indexable Row tuples in SQLAlchemy; keyword
        # order is the column order the fake hands back.
        return list(self.__dict__.values())[i]


class _Team(_Row):
    pass


class _Progress(_Row):
    pass


class _Cell(_Row):
    pass


class _CellDone(_Row):
    pass


class _Ledger(_Row):
    pass


class _Channel(_Row):
    pass


class _Member(_Row):
    pass


def _columns(cls, *names):
    for name in names:
        setattr(cls, name, _Col(cls, name))


_columns(_Team, "id", "event_id", "score")
_columns(_Progress, "id", "event_id", "task_id", "team_id")
_columns(_Cell, "id", "event_id", "task_id")
_columns(_CellDone, "id", "cell_id", "team_id")
_columns(_Ledger, "id", "event_id", "task_id", "team_id", "source_type", "status")
_columns(_Channel, "id", "event_id", "kind", "channel_id")
_columns(_Member, "player_id", "team_id")

_MODELS = {
    "EventTeam": _Team, "EventProgress": _Progress, "EventBingoCell": _Cell,
    "EventBingoCompletion": _CellDone, "EventCompletion": _Ledger,
    "EventChannel": _Channel, "EventTeamMember": _Member,
}


class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *preds):
        rows = self._rows
        for p in preds:
            if isinstance(p, _Pred):
                rows = [r for r in rows if getattr(r, p.name, None) == p.value]
        return _Q(rows)

    def with_for_update(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def distinct(self, *a, **k):
        return self

    def limit(self, n):
        return _Q(self._rows[:n])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _Sess:
    """Model-dispatched session: ``query(Model)`` (or ``query(Model.col)``)
    sees that model's live rows and ``add()`` lands new rows in the same store
    — autoflush is what makes a just-inserted bingo completion visible to the
    bonus evaluation that follows it. Team rows come back ordered the way
    ``ORDER BY score DESC, id ASC`` orders them."""

    def __init__(self, store):
        self.store = store

    def query(self, *args):
        model = getattr(args[0], "_model", args[0])
        rows = self.store.setdefault(model, [])
        if model is _Team:
            rows = sorted(rows, key=lambda t: (-float(t.score or 0), t.id))
        return _Q(rows)

    def add(self, obj):
        self.store.setdefault(type(obj), []).append(obj)

    def flush(self):
        pass


class _FakeRedis:
    """The one command ``_claim_lead_change`` issues: ``SET key val EX ttl GET``.
    Reads come back as bytes, like the real (undecoded) connection."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.ttls = {}

    def set(self, key, value, ex=None, get=False):
        previous = self.values.get(key)
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        if not get:
            return True
        return previous.encode() if previous is not None else None


@contextmanager
def _redis(client):
    """Point ``utils.redis.redis_client`` (a conftest MagicMock) at ``client``.
    Every lead-change path runs inside one of these so the cross-lane claim is
    exercised deterministically instead of against an auto-created mock."""
    module = sys.modules["utils.redis"]
    saved = getattr(module, "redis_client", None)
    module.redis_client = SimpleNamespace(client=client)
    try:
        yield client
    finally:
        module.redis_client = saved


def _watermark_key(event_id=None):
    return engine.LEAD_WATERMARK_KEY.format(
        event_id=EVENT_ID if event_id is None else event_id)


@contextmanager
def _fake_models():
    dbm = sys.modules["db.models"]
    saved = {name: getattr(dbm, name) for name in _MODELS}
    for name, cls in _MODELS.items():
        setattr(dbm, name, cls)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(dbm, name, value)


@contextmanager
def _patched(**fns):
    saved = {name: getattr(engine, name) for name in fns}
    for name, fn in fns.items():
        setattr(engine, name, fn)
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(engine, name, fn)


@contextmanager
def _team_interest(value):
    """Stub ``services.event_team_discord.team_channel_interest`` (the
    per-team-channel half of the routing check)."""
    module = sys.modules.get("services.event_team_discord")
    created = module is None
    if created:
        module = ModuleType("services.event_team_discord")
        sys.modules["services.event_team_discord"] = module
    saved = getattr(module, "team_channel_interest", None)
    module.team_channel_interest = lambda *a, **k: value
    try:
        yield
    finally:
        if created:
            sys.modules.pop("services.event_team_discord", None)
        elif saved is not None:
            module.team_channel_interest = saved


EVENT_ID = 1
TASK_ID = 9
# 2×2 board: cells 0+1 are row r0, so a team holding cell 0 completes a line
# the moment its second tile lands.
CELL_IDS = {idx: 10 + idx for idx in range(4)}


def _apply(*, teams, acting_team, task_points=2, line_points=0,
           has_bingo=False, done_idxs=(), routable=True, completion_id=555,
           watermark=None):
    """Drive one completion through ``apply_ledger_row``.

    Returns ``(captured, teams_by_id)`` where ``captured`` is the ordered list
    of ``(notification_type, player_id, payload)`` the apply enqueued.

    ``watermark`` is the shared :class:`_FakeRedis` when a test needs several
    applies to see one another's claims; the default is a fresh one, i.e. an
    event whose lead has never been announced.
    """
    team_rows = [_Team(id=tid, event_id=EVENT_ID, score=score, name=f"T{tid}")
                 for tid, score in teams]
    cells = [_Cell(id=CELL_IDS[i], event_id=EVENT_ID, idx=i, label=f"c{i}",
                   task_id=TASK_ID if i == 1 else None)
             for i in range(4)] if has_bingo else []
    store = {
        _Team: team_rows,
        _Progress: [_Progress(event_id=EVENT_ID, task_id=TASK_ID, team_id=acting_team,
                              progress=0, completed=False, completed_at=None)],
        _Cell: cells,
        _CellDone: [_CellDone(cell_id=CELL_IDS[i], team_id=acting_team, player_id=None)
                    for i in done_idxs],
        _Ledger: [],
        _Channel: ([_Channel(id=1, event_id=EVENT_ID, kind="leaderboard",
                             channel_id="4242")] if routable else []),
        _Member: [],
    }
    event = {"id": EVENT_ID, "name": "Ev", "group_id": 42, "kind": "standard",
             "has_bingo": has_bingo, "board_size": 2, "message_config": None,
             "bonus_line_points": line_points, "bonus_blackout_points": 0}
    task = {"id": TASK_ID, "type": "item", "target_value": 1,
            "points": task_points, "label": "A tile", "config": None}
    completion = SimpleNamespace(
        id=completion_id, event_id=EVENT_ID, task_id=TASK_ID, team_id=acting_team,
        player_id=5, quantity=1, matched_target=None, source_type="drop",
        proof_url=None, note=None)
    captured = []
    with _fake_models(), _team_interest(False), _redis(
        watermark if watermark is not None else _FakeRedis()
    ), _patched(
        _enqueue_notification=lambda s, nt, ev, pid, data: captured.append((nt, pid, data)),
        _publish=lambda *a, **k: None,
        _task_contributors=lambda *a, **k: [],
        _award_contribution_points=lambda *a, **k: None,
    ):
        engine.apply_ledger_row(
            _Sess(store), None, event, task, completion,
            cells=[{"id": CELL_IDS[1], "idx": 1, "label": "c1"}] if has_bingo else None,
            player_name="Zezima")
    return captured, {t.id: t for t in team_rows}


def _lead_changes(captured):
    return [data for nt, _pid, data in captured if nt == "event_lead_change"]


# ── The regression ───────────────────────────────────────────────────────────

class TestBonusDrivenOvertake:
    def test_line_bonus_overtake_announces(self):
        # T2 trails 95–100. Its tile is worth 2 (→97, still behind) but
        # completes row r0 for another 10 (→107). The pre-t60 comparison closed
        # after the +2 and never saw the overtake — this is the silent case
        # from the report.
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=2, line_points=10,
                                 has_bingo=True, done_idxs=(0,))
        assert teams[2].score == 107
        leads = _lead_changes(captured)
        assert len(leads) == 1
        assert leads[0]["team_id"] == 2
        assert leads[0]["team_score"] == 107
        assert leads[0]["previous_team_id"] == 1

    def test_zero_point_task_overtake_announces(self):
        # Same board, tile worth nothing: the whole overtake is the line bonus.
        # The old gate never evaluated the leader for a 0-point task at all.
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=0, line_points=10,
                                 has_bingo=True, done_idxs=(0,))
        assert teams[2].score == 105
        leads = _lead_changes(captured)
        assert len(leads) == 1 and leads[0]["team_id"] == 2

    def test_line_bonus_that_does_not_overtake_is_silent(self):
        captured, teams = _apply(teams=((1, 200), (2, 95)), acting_team=2,
                                 task_points=2, line_points=10,
                                 has_bingo=True, done_idxs=(0,))
        assert teams[2].score == 107
        assert _lead_changes(captured) == []

    def test_bonus_overtake_announces_against_a_live_watermark(self):
        # Same two cases with the incumbent's lead already announced (the
        # steady state on a running event) — the cross-lane claim must not turn
        # the regression back on.
        seeded = _FakeRedis({_watermark_key(): "1"})
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=2, line_points=10, has_bingo=True,
                                 done_idxs=(0,), watermark=seeded)
        assert teams[2].score == 107
        assert [d["team_id"] for d in _lead_changes(captured)] == [2]
        assert seeded.values[_watermark_key()] == "2"

    def test_zero_point_overtake_announces_against_a_live_watermark(self):
        seeded = _FakeRedis({_watermark_key(): "1"})
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=0, line_points=10, has_bingo=True,
                                 done_idxs=(0,), watermark=seeded)
        assert teams[2].score == 105
        assert [d["team_id"] for d in _lead_changes(captured)] == [2]


class TestNoSpuriousAnnouncements:
    def test_leader_extending_its_lead_is_silent(self):
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=1,
                                 task_points=5)
        assert teams[1].score == 105
        assert _lead_changes(captured) == []
        # The completion itself still posts.
        assert [nt for nt, _p, _d in captured] == ["event_completion"]

    def test_trailing_team_staying_behind_is_silent(self):
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=2)
        assert teams[2].score == 97
        assert _lead_changes(captured) == []

    def test_one_announcement_per_handover(self):
        captured, _teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                  task_points=20, line_points=10,
                                  has_bingo=True, done_idxs=(0,))
        # Points AND bonus both moved the score past T1 — still ONE message.
        assert len(_lead_changes(captured)) == 1


class TestTieSemantics:
    def test_tying_the_lead_announces_nothing(self):
        # A shared top score has no sole leader: nobody took the lead.
        captured, teams = _apply(teams=((1, 100), (2, 95)), acting_team=2,
                                 task_points=5)
        assert teams[2].score == 100
        assert _lead_changes(captured) == []

    def test_breaking_the_tie_announces_the_winner(self):
        captured, teams = _apply(teams=((1, 100), (2, 100)), acting_team=2,
                                 task_points=5)
        assert teams[2].score == 105
        leads = _lead_changes(captured)
        assert len(leads) == 1
        assert leads[0]["team_id"] == 2 and leads[0]["previous_team_id"] is None


class TestPayloadIsDedupeProof:
    def test_repeat_handover_at_the_same_score_is_a_distinct_row(self):
        # notification_queue is uniquely indexed on (type, player, group, data)
        # and _enqueue_notification swallows the IntegrityError, so two
        # byte-identical payloads = one message. The same team retaking the
        # lead at the same score must still differ.
        first = _lead_changes(_apply(teams=((1, 100), (2, 95)), acting_team=2,
                                     task_points=10)[0])[0]
        second = _lead_changes(_apply(teams=((1, 100), (2, 95)), acting_team=2,
                                      task_points=10)[0])[0]
        assert first["team_score"] == second["team_score"] == 105
        assert (json.dumps(first, sort_keys=True, default=str)
                != json.dumps(second, sort_keys=True, default=str))

    def test_payload_carries_score_and_ledger_row(self):
        lead = _lead_changes(_apply(teams=((1, 100), (2, 95)), acting_team=2,
                                    task_points=10, completion_id=8271)[0])[0]
        assert lead["team_score"] == 105
        assert lead["completion_id"] == 8271
        assert lead["lead_reason"] == "completion"
        assert lead["at"]


# ── The shared helper, driven directly ───────────────────────────────────────

class TestAnnounceLeadChange:
    EVENT = {"id": EVENT_ID, "name": "Ev", "group_id": 42}

    def _session(self, teams, channels=True, members=()):
        return _Sess({
            _Team: [_Team(id=tid, event_id=EVENT_ID, score=score)
                    for tid, score in teams],
            _Channel: ([_Channel(id=1, event_id=EVENT_ID, kind="announcements",
                                 channel_id="7")] if channels else []),
            _Member: [_Member(player_id=pid, team_id=tid) for pid, tid in members],
        })

    def _run(self, session, previous, player_id=None, watermark=None, **kw):
        captured = []
        with _fake_models(), _team_interest(False), _redis(
            watermark if watermark is not None else _FakeRedis()
        ), _patched(
            _enqueue_notification=lambda s, nt, ev, pid, data: captured.append((nt, pid, data)),
        ):
            engine._announce_lead_change(session, self.EVENT, previous, player_id, **kw)
        return captured

    def test_no_snapshot_means_no_comparison(self):
        session = self._session(((1, 100), (2, 50)))
        assert self._run(session, engine._NO_LEAD_SNAPSHOT) == []

    def test_handover_caused_by_someone_elses_loss_announces(self):
        # A revoke drops T2 from 110 to 90 without touching T1: the lead moved
        # even though the team that scored is not the new leader.
        session = self._session(((1, 100), (2, 90)))
        captured = self._run(session, (2, 110), player_id=5, reason="revoke")
        assert len(captured) == 1
        _nt, _pid, data = captured[0]
        assert data["team_id"] == 1 and data["previous_team_id"] == 2
        assert data["lead_reason"] == "revoke"

    def test_representative_player_stands_in_for_admin_actions(self):
        # notification_queue.player_id is NOT NULL; an admin edit has no actor,
        # so one is borrowed from the roster of the team that took the lead.
        session = self._session(((1, 100), (2, 90)), members=((77, 1),))
        captured = self._run(session, (2, 110), reason="task_edit")
        assert [pid for _nt, pid, _d in captured] == [77]

    def test_unroutable_lead_change_is_logged(self, caplog):
        # (G) event_lead_change targets 'leaderboard' → 'announcements'; with
        # neither configured the sender parks it as 'skipped' while completions
        # keep posting, which reads as "lead changes don't work".
        session = self._session(((1, 100), (2, 90)), channels=False)
        with caplog.at_level(logging.WARNING):
            captured = self._run(session, (2, 110), player_id=5)
        assert len(captured) == 1  # still enqueued (the in-game inbox wants it)
        assert any("no leaderboard/announcements channel" in r.getMessage()
                   for r in caplog.records)

    def test_admin_corrections_announce_only_while_the_event_lives(self):
        # Deliberate scope for revoke / task-edit re-folds: a finished event's
        # standings are settled and retro cleanups (dedupe_multipath_drops)
        # must not resurrect its channel weeks later.
        assert engine._lead_changes_announceable(SimpleNamespace(status="active"))
        assert engine._lead_changes_announceable(SimpleNamespace(status="draft"))
        assert engine._lead_changes_announceable(SimpleNamespace()) is True
        assert engine._lead_changes_announceable(SimpleNamespace(status="past")) is False

    def test_routable_lead_change_logs_nothing(self, caplog):
        session = self._session(((1, 100), (2, 90)))
        with caplog.at_level(logging.WARNING):
            self._run(session, (2, 110), player_id=5)
        assert not [r for r in caplog.records
                    if "no leaderboard/announcements channel" in r.getMessage()]


# ── The cross-lane claim ─────────────────────────────────────────────────────

class TestCrossLaneClaim:
    """``workers/event_consumer`` applies on LANES=4 concurrent threads, each on
    its own READ COMMITTED session, so a lane's post-apply standings read sees
    the other lanes' COMMITTED score writes. The snapshot compare alone only
    says "the lead moved since this transaction started" — true for the lane
    that caused it AND for every lane that merely observed it, each of which
    would post its own copy stamped with its own unrelated task.
    """

    EVENT = {"id": EVENT_ID, "name": "Ev", "group_id": 42}

    def _session(self, teams):
        return _Sess({
            _Team: [_Team(id=tid, event_id=EVENT_ID, score=score)
                    for tid, score in teams],
            _Channel: [_Channel(id=1, event_id=EVENT_ID, kind="leaderboard",
                                channel_id="7")],
            _Member: [],
        })

    def _run(self, session, previous, watermark, **kw):
        captured = []
        with _fake_models(), _team_interest(False), _redis(watermark), _patched(
            _enqueue_notification=lambda s, nt, ev, pid, data: captured.append((nt, pid, data)),
        ):
            engine._announce_lead_change(session, self.EVENT, previous, 5, **kw)
        return captured

    def test_one_handover_announces_exactly_once(self):
        # T1=100, T2=95, T3=10. Lane 2 commits T2=110 (the real overtake); lane
        # 0 is concurrently applying +5 for T3 and opened its snapshot before
        # that commit, so its post-apply read sees T2 leading as well.
        shared = _FakeRedis()
        session = self._session(((1, 100), (2, 110), (3, 15)))
        lane2 = self._run(session, (1, 100), shared, reason="completion",
                          extra={"task_id": 7, "task_label": "the tile that won it"})
        lane0 = self._run(session, (1, 100), shared, reason="completion",
                          extra={"task_id": 8, "task_label": "T3's unrelated tile"})
        assert len(lane2) == 1 and lane0 == []
        assert lane2[0][2]["team_id"] == 2
        assert lane2[0][2]["task_label"] == "the tile that won it"

    def test_observer_of_an_announced_handover_is_silent(self):
        # Same race with the lanes the other way round: the winner already
        # stamped the watermark, so the observing lane finds its own hand-over
        # spoken for.
        session = self._session(((1, 100), (2, 110), (3, 15)))
        captured = self._run(session, (1, 100),
                             _FakeRedis({_watermark_key(): "2"}),
                             extra={"task_id": 8, "task_label": "T3's tile"})
        assert captured == []

    def test_announcing_stamps_the_watermark_with_a_ttl(self):
        fake = _FakeRedis()
        self._run(self._session(((1, 100), (2, 90))), (2, 110), fake)
        assert fake.values[_watermark_key()] == "1"
        assert fake.ttls[_watermark_key()] == engine.LEAD_WATERMARK_TTL_SECONDS

    def test_lead_handed_back_is_announceable_again(self):
        # A revoke returns the lead to the team the watermark already named
        # once. The watermark holds the LAST announced leader, not "every team
        # that ever led", so the hand-over back is a change and posts.
        shared = _FakeRedis()
        first = self._run(self._session(((1, 100), (2, 110))), (1, 100), shared)
        second = self._run(self._session(((1, 100), (2, 90))), (2, 110), shared,
                           reason="revoke")
        assert [c[2]["team_id"] for c in first + second] == [2, 1]

    def test_an_unchanged_lead_never_reaches_redis(self):
        # Every apply on a bingo event opens the snapshot window, including
        # zero-point tiles — the no-change case has to stay query-free.
        fake = _FakeRedis()
        assert self._run(self._session(((1, 100), (2, 50))), (1, 95), fake) == []
        assert fake.values == {}


class TestClaimDegradesSafely:
    """Redis is best-effort here: silence is the bug t60 fixed, so every
    degraded path announces."""

    def test_no_redis_handle_announces(self):
        with _redis(None):
            assert engine._claim_lead_change(EVENT_ID, 2) is True

    def test_redis_error_announces(self):
        class _Broken:
            def set(self, *a, **k):
                raise ConnectionError("redis down")

        with _redis(_Broken()):
            assert engine._claim_lead_change(EVENT_ID, 2) is True

    def test_missing_key_announces_and_seeds(self):
        # Fresh event / worker restart / TTL expiry: the caller has already
        # proved from its own before/after snapshot that the lead moved.
        fake = _FakeRedis()
        with _redis(fake):
            assert engine._claim_lead_change(EVENT_ID, 2) is True
            assert engine._claim_lead_change(EVENT_ID, 2) is False

    def test_claims_are_per_event(self):
        fake = _FakeRedis()
        with _redis(fake):
            assert engine._claim_lead_change(EVENT_ID, 2) is True
            assert engine._claim_lead_change(EVENT_ID + 1, 2) is True
        assert fake.values[_watermark_key(EVENT_ID + 1)] == "2"

    def test_see_saw_announces_every_swing(self):
        fake = _FakeRedis()
        with _redis(fake):
            claims = [engine._claim_lead_change(EVENT_ID, tid)
                      for tid in (2, 1, 2, 2)]
        assert claims == [True, True, True, False]


# ── Payload the plugin can actually parse ────────────────────────────────────

class TestPluginSafePayload:
    def test_team_score_is_an_int_on_a_fractional_score(self):
        # loot_sweep scores carry 2dp. EventNotification.Data.teamScore is an
        # Integer and the plugin parses the drained batch in ONE fromJson, so a
        # fractional value throws and eats every unrelated notification with it.
        session = _Sess({
            _Team: [_Team(id=1, event_id=EVENT_ID, score=105.75),
                    _Team(id=2, event_id=EVENT_ID, score=90)],
            _Channel: [_Channel(id=1, event_id=EVENT_ID, kind="leaderboard",
                                channel_id="7")],
            _Member: [],
        })
        captured = []
        with _fake_models(), _team_interest(False), _redis(_FakeRedis()), _patched(
            _enqueue_notification=lambda s, nt, ev, pid, data: captured.append((nt, pid, data)),
        ):
            engine._announce_lead_change(
                session, {"id": EVENT_ID, "name": "Ev", "group_id": 42},
                (2, 110), 5, reason="revoke")
        score = captured[0][2]["team_score"]
        assert isinstance(score, int) and score == 106

    def test_team_score_is_an_int_on_the_completion_path(self):
        lead = _lead_changes(_apply(teams=((1, 100), (2, 95)), acting_team=2,
                                    task_points=10)[0])[0]
        assert isinstance(lead["team_score"], int)
