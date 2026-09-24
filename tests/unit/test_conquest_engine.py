"""services/conquest_engine.py against a real (SQLite, in-memory) database.

The conftest stubs ``db``/``services`` with MagicMocks, which can't say
anything about SQL, so this module builds its own: the REAL event and Conquest
model modules are loaded under a private package whose ``base`` is a fresh
declarative Base, the handful of external tables they reference are stubbed,
and ``db.models`` / ``services.conquest`` / ``services.event_engine`` are
swapped in per test. The engine then runs its actual queries.
"""

import importlib.util
import json
import os
import random
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, String, Table, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PKG = "_conquest_models_ut"


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_models():
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []
    sys.modules[_PKG] = pkg
    base = types.ModuleType(f"{_PKG}.base")
    base.Base = declarative_base()
    sys.modules[f"{_PKG}.base"] = base
    meta = base.Base.metadata
    # External tables the event models point at.
    Table("groups", meta, Column("group_id", Integer, primary_key=True))
    Table("users", meta, Column("user_id", Integer, primary_key=True))
    Table("npc_list", meta, Column("npc_id", Integer, primary_key=True))
    Table("subscription_tiers", meta, Column("key", String(40), primary_key=True))

    class Player(base.Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        player_name = Column(String(64))

    events = _load(f"{_PKG}.events", "db", "models", "events.py")
    conquest = _load(f"{_PKG}.event_conquest", "db", "models", "event_conquest.py")
    models = types.SimpleNamespace(Player=Player)
    for mod in (events, conquest):
        for key, value in vars(mod).items():
            if isinstance(value, type) and hasattr(value, "__tablename__"):
                setattr(models, key, value)
    return base.Base, models


BASE, M = _build_models()
cq = _load("_conquest_rules_engine_ut", "services", "conquest.py")
engine_mod = _load("_conquest_engine_ut", "services", "conquest_engine.py")

T0 = datetime(2026, 10, 1, 12, 0, 0)


class FakeEventEngine:
    """The bits of services.event_engine the Conquest engine calls."""

    def __init__(self):
        self.published = []
        self.enqueued = []
        self.lead_changes = []

    def _publish(self, event_id, frame):
        self.published.append(frame)

    def _enqueue_notification(self, session, ntype, event, player_id, data):
        self.enqueued.append((ntype, player_id, data))

    def _team_representative_player(self, session, team_id):
        return 999

    def _event_to_dict(self, event):
        return {"id": event.id, "name": event.name, "group_id": event.group_id,
                "kind": event.kind, "message_config": None}

    def _leader_snapshot(self, session, event):
        return "before"

    def _announce_lead_change(self, session, event, previous, *a, **k):
        self.lead_changes.append((event["id"], previous, k.get("reason")))

    def types(self, ntype):
        return [d for t, _p, d in self.enqueued if t == ntype]


@pytest.fixture
def env(monkeypatch):
    sql = create_engine("sqlite://")
    BASE.metadata.create_all(sql)
    session = sessionmaker(bind=sql)()
    fake_engine = FakeEventEngine()
    db_pkg = types.ModuleType("db")
    db_pkg.models = M
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.models", M)
    services = sys.modules.get("services")
    monkeypatch.setitem(sys.modules, "services.conquest", cq)
    monkeypatch.setitem(sys.modules, "services.event_engine", fake_engine)
    if services is not None:
        monkeypatch.setattr(services, "conquest", cq, raising=False)
        monkeypatch.setattr(services, "event_engine", fake_engine, raising=False)
    yield SimpleNamespace(s=session, ev=fake_engine)
    session.close()


def _event(s, **over):
    ev = M.Event(id=over.pop("id", 1), name="War", status=over.pop("status", "active"),
                 kind="conquest", starts_at=T0, activated_at=T0,
                 ends_at=T0 + timedelta(days=7), **over)
    s.add(ev)
    s.flush()
    return ev


def _teams(s, ev, *names):
    out = []
    for name in names:
        team = M.EventTeam(event_id=ev.id, name=name, score=0)
        s.add(team)
        s.flush()
        out.append(team)
    return out


def _map(s, ev, *, regions=(("r1", 3.0),), tiles=(("Zulrah", "r1"),), settings=None,
         seeded=True):
    """Regions + tiles, each tile with one kc rule (target 10, 1 troop).
    Returns ({region key: row}, [tile rows], [task rows])."""
    s.add(M.ConquestMap(event_id=ev.id, settings=json.dumps(settings or {}),
                        revision=0, seeded_at=T0 if seeded else None))
    region_rows = {}
    for i, (key, bonus) in enumerate(regions):
        row = M.ConquestRegion(event_id=ev.id, name=key.title(), bonus=bonus, sort=i)
        s.add(row)
        s.flush()
        region_rows[key] = row
    tile_rows, task_rows = [], []
    for i, (label, region_key) in enumerate(tiles):
        tile = M.ConquestTile(event_id=ev.id, idx=i, label=label, x=0.5, y=0.5,
                              region_id=region_rows[region_key].id if region_key else None)
        s.add(tile)
        s.flush()
        task = M.EventTask(event_id=ev.id, type="kc_target", label=f"10 {label} kills",
                           target=label, target_value=10, points=0)
        s.add(task)
        s.flush()
        s.add(M.ConquestRule(event_id=ev.id, tile_id=tile.id, task_id=task.id, troops=1))
        tile_rows.append(tile)
        task_rows.append(task)
    s.flush()
    return region_rows, tile_rows, task_rows


def _apply(env, ev, task, team, qty, *, rng=None, player_id=5, cid=1, when=None):
    completion = SimpleNamespace(id=cid, team_id=team.id, player_id=player_id,
                                 quantity=qty, matched_target=None)
    task_dict = {"id": task.id, "label": task.label, "target_value": task.target_value}
    event_dict = {"id": ev.id, "name": ev.name, "group_id": None, "kind": "conquest"}
    return engine_mod.apply_conquest(env.s, None, event_dict, task_dict, completion,
                                     player_name="Zed", rng=rng or random.Random(1),
                                     now=when or T0 + timedelta(hours=1))


class _Faces:
    def __init__(self, faces):
        self.faces = list(faces)

    def randint(self, lo, hi):
        return self.faces.pop(0)


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
class TestApply:
    def test_below_threshold_only_progresses(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, (tile,), (task,) = _map(env.s, ev)
        out = _apply(env, ev, task, red, 7)
        assert out["troops"] == 0 and out["progress"] == 7 and out["target"] == 10
        assert env.ev.published[-1]["kind"] == "conquest_progress"
        env.s.refresh(tile)
        assert tile.owner_team_id is None

    def test_neutral_claim(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, (tile,), (task,) = _map(env.s, ev)
        out = _apply(env, ev, task, red, 10)
        assert out["troops"] == 1 and out["outcomes"] == ["claim"] and out["captured"]
        env.s.refresh(tile)
        assert (tile.owner_team_id, tile.defense, tile.captures) == (red.id, 1, 1)
        holds = env.s.query(M.ConquestHold).all()
        assert [(h.team_id, h.ended_at) for h in holds] == [(red.id, None)]
        capture = env.ev.types("event_conquest_capture")[0]
        assert "claimed **Zulrah**" in capture["conquest_headline"]
        assert "holds 1 tile" in capture["conquest_detail_line"]
        # The only tile of its region: control changes hands too.
        region = env.ev.types("event_conquest_region")[0]
        assert "now controls" in region["conquest_headline"]

    def test_progress_carries_over_between_applies(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, _tiles, (task,) = _map(env.s, ev)
        assert _apply(env, ev, task, red, 6)["troops"] == 0
        assert _apply(env, ev, task, red, 6, cid=2)["troops"] == 1
        progress = env.s.query(M.EventProgress).one()
        assert progress.progress == 12 and not progress.completed

    def test_big_fold_sends_several_troops(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, (tile,), (task,) = _map(env.s, ev)
        out = _apply(env, ev, task, red, 35)
        assert out["troops"] == 3 and out["outcomes"] == ["claim", "fortify", "fortify"]
        env.s.refresh(tile)
        assert tile.defense == 3
        assert env.s.query(M.ConquestTroops).one().earned == 3

    def test_breach_then_capture_moves_the_hold(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _r, (tile,), (task,) = _map(env.s, ev)
        _apply(env, ev, task, red, 10)                      # Red claims, defense 1
        out = _apply(env, ev, task, blue, 10, cid=2, rng=_Faces([6, 6, 1]))
        assert out["outcomes"] == ["breach"]
        battle = env.ev.types("event_conquest_battle")[0]
        assert "broke through" in battle["conquest_headline"]
        assert "`6 · 6` vs `1`" in battle["conquest_dice_line"]
        out = _apply(env, ev, task, blue, 10, cid=3,
                     when=T0 + timedelta(hours=2))
        assert out["outcomes"] == ["capture"]
        env.s.refresh(tile)
        assert tile.owner_team_id == blue.id and tile.defense == 1
        holds = sorted(env.s.query(M.ConquestHold).all(), key=lambda h: h.id)
        assert holds[0].team_id == red.id and holds[0].ended_at == T0 + timedelta(hours=2)
        assert holds[1].team_id == blue.id and holds[1].ended_at is None
        capture = env.ev.types("event_conquest_capture")[-1]
        assert "from **Red**" in capture["conquest_headline"]

    def test_region_needs_every_tile(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        regions, tiles, tasks = _map(env.s, ev, tiles=(("A", "r1"), ("B", "r1")))
        _apply(env, ev, tasks[0], red, 10)
        env.s.refresh(regions["r1"])
        assert regions["r1"].owner_team_id is None
        _apply(env, ev, tasks[1], red, 10, cid=2)
        env.s.refresh(regions["r1"])
        assert regions["r1"].owner_team_id == red.id
        assert len(env.ev.types("event_conquest_region")) == 1

    def test_task_without_rule_just_accumulates(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        stray = M.EventTask(event_id=ev.id, type="kc_target", label="x", target="x",
                            target_value=5, points=0)
        env.s.add(stray)
        env.s.flush()
        out = _apply(env, ev, stray, red, 20)
        assert out["troops"] == 0
        assert env.s.query(M.ConquestBattle).count() == 0


# --------------------------------------------------------------------------- #
# Revoke
# --------------------------------------------------------------------------- #
class TestRevoke:
    def test_revoked_troop_becomes_debt_paid_by_the_next(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, (tile,), (task,) = _map(env.s, ev)
        for cid, status in ((1, "auto"), (2, "revoked")):
            env.s.add(M.EventCompletion(id=cid, event_id=ev.id, task_id=task.id,
                                        team_id=red.id, status=status, quantity=10))
        env.s.flush()
        _apply(env, ev, task, red, 10, cid=1)
        _apply(env, ev, task, red, 10, cid=2)               # claim, then fortify
        event_dict = {"id": ev.id, "kind": "conquest"}
        task_dict = {"id": task.id, "target_value": 10}
        out = engine_mod.revoke_conquest(env.s, event_dict, task_dict, red.id, None)
        assert out["troop_debt"] == 1 and out["progress"] == 10
        book = env.s.query(M.ConquestTroops).one()
        assert (book.debt, book.earned) == (1, 1)
        out = _apply(env, ev, task, red, 10, cid=3)
        assert out["troops"] == 1 and out["troop_debt_paid"] == 1
        assert out.get("outcomes") == []
        env.s.refresh(tile)
        assert tile.defense == 2  # the paid-off troop did nothing


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
class TestSeed:
    def test_dealt_start_is_even_and_idempotent(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _map(env.s, ev, tiles=[(f"T{i}", "r1") for i in range(6)],
             settings={"start_mode": "dealt", "start_defense": 2}, seeded=False)
        out = engine_mod.seed_conquest(env.s, ev, now=T0, rng=random.Random(3))
        assert out == {"seeded": True, "dealt": 6}
        owners = [t.owner_team_id for t in env.s.query(M.ConquestTile).all()]
        assert owners.count(red.id) == 3 and owners.count(blue.id) == 3
        assert {t.defense for t in env.s.query(M.ConquestTile).all()} == {2}
        assert env.s.query(M.ConquestHold).count() == 6
        assert engine_mod.seed_conquest(env.s, ev, now=T0) == {"seeded": False}

    def test_neutral_start_resets_leftovers(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, (tile,), _t = _map(env.s, ev, settings={"neutral_defense": 2}, seeded=False)
        tile.owner_team_id = red.id
        env.s.add(M.ConquestHold(event_id=ev.id, tile_id=tile.id, team_id=red.id,
                                 started_at=T0))
        env.s.flush()
        engine_mod.seed_conquest(env.s, ev, now=T0)
        env.s.refresh(tile)
        assert tile.owner_team_id is None and tile.defense == 2
        assert env.s.query(M.ConquestHold).count() == 0


class TestSettle:
    def test_hold_time_scores_and_lead_change(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _r, tiles, _t = _map(env.s, ev, regions=(("r1", 3.0),),
                             tiles=(("A", "r1"), ("B", None)))
        tiles[0].owner_team_id = red.id
        tiles[1].owner_team_id = blue.id
        env.s.add_all([
            M.ConquestHold(event_id=ev.id, tile_id=tiles[0].id, team_id=red.id,
                           started_at=T0),
            M.ConquestHold(event_id=ev.id, tile_id=tiles[1].id, team_id=blue.id,
                           started_at=T0 + timedelta(hours=5)),
        ])
        env.s.flush()
        out = engine_mod.settle_conquest(env.s, None, ev, now=T0 + timedelta(hours=10))
        assert out == {"settled": True, "changed": True}
        env.s.refresh(red)
        env.s.refresh(blue)
        # Red: tile A 10h at 1 + region r1 (only tile A) 10h at 3 = 40.
        assert red.score == 40 and blue.score == 5
        assert env.ev.lead_changes == [(ev.id, "before", "conquest")]

    def test_final_mode_uses_the_map_now(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, tiles, _t = _map(env.s, ev, settings={"scoring_mode": "final"},
                             tiles=(("A", "r1"), ("B", "r1")))
        for t in tiles:
            t.owner_team_id = red.id
        env.s.flush()
        engine_mod.settle_conquest(env.s, None, ev, now=T0 + timedelta(hours=1))
        env.s.refresh(red)
        assert red.score == 5  # 2 tiles + region bonus 3

    def test_unseeded_map_is_skipped(self, env):
        ev = _event(env.s)
        _teams(env.s, ev, "Red")
        _map(env.s, ev, seeded=False)
        assert engine_mod.settle_conquest(env.s, None, ev, now=T0) == {"settled": False}

    def test_summary_posts_on_cadence(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _map(env.s, ev, settings={"summary_hours": 6})
        env.s.query(M.ConquestMap).update({M.ConquestMap.summary_at: T0})
        engine_mod.settle_conquest(env.s, None, ev, now=T0 + timedelta(hours=5))
        assert env.ev.types("event_conquest_summary") == []
        engine_mod.settle_conquest(env.s, None, ev, now=T0 + timedelta(hours=6))
        summary = env.ev.types("event_conquest_summary")
        assert len(summary) == 1 and "**Red**" in summary[0]["conquest_summary_block"]

    def test_final_standings_rank(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _r, tiles, _t = _map(env.s, ev, settings={"scoring_mode": "final"},
                             tiles=(("A", "r1"), ("B", None), ("C", None)))
        tiles[0].owner_team_id = red.id
        tiles[1].owner_team_id = blue.id
        tiles[2].owner_team_id = blue.id
        env.s.flush()
        out = engine_mod.conquest_final_standings(env.s, ev)
        assert [(r["name"], r["score"], r["tiles"]) for r in out] == [
            ("Red", 4, 1), ("Blue", 2, 2)]


class TestBlockers:
    def test_empty_map(self, env):
        ev = _event(env.s, status="draft")
        _teams(env.s, ev, "Red", "Blue")
        codes = [b["code"] for b in engine_mod.conquest_blockers(env.s, ev)]
        assert codes == ["conquest_no_map"]

    def test_tile_without_rule_and_one_team(self, env):
        ev = _event(env.s, status="draft")
        _teams(env.s, ev, "Red")
        _map(env.s, ev, tiles=(("A", "r1"), ("B", "r1")), seeded=False)
        bare = M.ConquestTile(event_id=ev.id, idx=9, label="Bare", x=0.1, y=0.1)
        env.s.add(bare)
        env.s.flush()
        codes = [b["code"] for b in engine_mod.conquest_blockers(env.s, ev)]
        assert codes == ["conquest_tiles_without_rules", "conquest_min_teams"]

    def test_deal_needs_a_tile_per_team(self, env):
        ev = _event(env.s, status="draft")
        _teams(env.s, ev, "A", "B", "C")
        _map(env.s, ev, tiles=(("X", "r1"), ("Y", "r1")),
             settings={"start_mode": "dealt"}, seeded=False)
        codes = [b["code"] for b in engine_mod.conquest_blockers(env.s, ev)]
        assert codes == ["conquest_deal_short"]


# --------------------------------------------------------------------------- #
# Team deletion, reads, admin adjust
# --------------------------------------------------------------------------- #
class TestForgetTeam:
    def test_frees_tiles_and_drops_holds(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        regions, (tile,), (task,) = _map(env.s, ev)
        _apply(env, ev, task, red, 10)
        engine_mod.forget_team_if_conquest(env.s, ev.id, red.id)
        env.s.refresh(tile)
        env.s.refresh(regions["r1"])
        assert tile.owner_team_id is None and regions["r1"].owner_team_id is None
        assert env.s.query(M.ConquestHold).count() == 0
        assert env.s.query(M.ConquestTroops).count() == 0

    def test_other_kinds_untouched(self, env):
        ev = _event(env.s)
        ev.kind = "bingo"
        env.s.flush()
        # No Conquest tables are touched: a bingo event never reaches them.
        engine_mod.forget_team_if_conquest(env.s, ev.id, 1)


class TestPayload:
    def test_shape_and_progress_to_next_troop(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _r, (tile,), (task,) = _map(env.s, ev)
        _apply(env, ev, task, red, 13)
        env.s.add(M.Player(player_id=5, player_name="Zed"))
        env.s.flush()
        out = engine_mod.conquest_payload(env.s, ev, now=T0 + timedelta(hours=2))
        assert out["seeded"] and out["settings"]["battle_mode"] == "dice"
        t = out["tiles"][0]
        assert t["owner_team_id"] == red.id and t["defense"] == 1
        assert t["rules"][0]["progress"] == {str(red.id): 3}
        assert t["troops"] == {str(red.id): 1}
        teams = {row["name"]: row for row in out["teams"]}
        assert teams["Red"]["tiles"] == 1 and teams["Red"]["live_score"] == 4.0
        assert out["battles"][0]["outcome"] == "claim"
        assert out["battles"][0]["player_name"] == "Zed"
        assert out["regions"][0]["tile_ids"] == [tile.id]

    def test_conceal_hides_rules(self, env):
        ev = _event(env.s)
        _teams(env.s, ev, "Red")
        _map(env.s, ev)
        out = engine_mod.conquest_payload(env.s, ev, conceal=True)
        assert out["tiles"][0]["rules"] == [] and out["rules_hidden"]

    def test_battles_page(self, env):
        ev = _event(env.s)
        red, = _teams(env.s, ev, "Red")
        _r, _tiles, (task,) = _map(env.s, ev)
        _apply(env, ev, task, red, 10)
        page = engine_mod.battles_page(env.s, ev.id, limit=10)
        assert [b["outcome"] for b in page["battles"]] == ["claim"]
        assert page["next_before"] is None


class TestAdjust:
    def test_hand_over_a_tile(self, env):
        ev = _event(env.s)
        red, blue = _teams(env.s, ev, "Red", "Blue")
        _r, (tile,), (task,) = _map(env.s, ev)
        _apply(env, ev, task, red, 10)
        out = engine_mod.adjust_tile(env.s, ev, tile.id, owner_team_id=blue.id,
                                     defense=3, now=T0 + timedelta(hours=3))
        assert out["owner_team_id"] == blue.id and out["defense"] == 3
        assert out["region_owner_team_id"] == blue.id
        admin = env.s.query(M.ConquestBattle).filter(
            M.ConquestBattle.source == "admin").one()
        assert admin.outcome == "adjust" and admin.owner_before == red.id

    def test_rejects_bad_input(self, env):
        ev = _event(env.s)
        _teams(env.s, ev, "Red")
        _r, (tile,), _t = _map(env.s, ev)
        with pytest.raises(engine_mod.AdjustError):
            engine_mod.adjust_tile(env.s, ev, tile.id, owner_team_id=12345)
        with pytest.raises(engine_mod.AdjustError):
            engine_mod.adjust_tile(env.s, ev, tile.id, owner_team_id=None, defense=99)
        with pytest.raises(engine_mod.AdjustError):
            engine_mod.adjust_tile(env.s, ev, 9999, owner_team_id=None)
