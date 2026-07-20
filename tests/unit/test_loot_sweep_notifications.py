"""Engine tests for the loot_sweep Discord verbosity enqueue
(services/event_engine.py::_apply_loot_sweep): which of the three sweep message
types fire for a receipt, and the toggle / item_min_points gating.

Driven through the REAL services/loot_sweep scoring + services/event_notifications
config (injected past the conftest stubs, like test_loot_sweep_engine.py). The
DB-shaped bits (_loot_sweep_score, contributors, standings, publish, enqueue)
are monkeypatched so the test exercises the branching, not SQLAlchemy.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _inject_real(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "services", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if "services" in sys.modules:
        setattr(sys.modules["services"], name.split(".")[-1], module)
    spec.loader.exec_module(module)
    return module


ls = _inject_real("services.loot_sweep", "loot_sweep.py")
_inject_real("services.event_notifications", "event_notifications.py")


# Fake ORM models the engine imports inside _apply_loot_sweep. Class attrs are
# needed so `EventProgress.task_id == …` filter expressions don't AttributeError.
class _Progress:
    task_id = team_id = event_id = progress = completed = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Team:
    id = event_id = score = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


_engine_spec = importlib.util.spec_from_file_location(
    "_loot_sweep_notif_ut", os.path.join(_ROOT, "services", "event_engine.py"))
engine = importlib.util.module_from_spec(_engine_spec)
sys.modules["_loot_sweep_notif_ut"] = engine
_engine_spec.loader.exec_module(engine)


KREE = {"kind": "loot_sweep", "decay_percent": 20, "set_bonus_points": 0,
        "groups": [{"label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40,
                    "items": [{"item_name": "Armadyl helmet", "points": 9},
                              {"item_name": "Armadyl chestplate", "points": 9},
                              {"item_name": "Armadyl hilt", "points": 13},
                              {"item_name": "Pet kree'arra", "points": 60,
                               "counts_for_group": False}]}]}


def _barrows():
    def bro(label, npc, prefix):
        return {"label": label, "npcs": [npc], "bonus_points": 4,
                "items": [{"item_name": f"{prefix} {c}", "points": 1} for c in "abcd"]}
    return {"kind": "loot_sweep", "decay_percent": 20, "set_bonus_points": 40,
            "groups": [bro("Ahrim", "Ahrim the Blighted", "ahrim"),
                       bro("Dharok", "Dharok the Wretched", "dharok")]}


def _apply(config, prev_counts, curr_counts, matched, message_config=None):
    """Run _apply_loot_sweep for one receipt; return {type: payload} enqueued."""
    cfg = ls.LootSweepConfig(config)
    prev_b, curr_b = ls.score_counts(prev_counts, cfg), ls.score_counts(curr_counts, cfg)
    captured = {}

    orig = {name: getattr(engine, name) for name in (
        "_enqueue_notification", "_loot_sweep_score", "_current_leader",
        "_task_contributors", "_award_contribution_points", "_publish",
        "_loot_sweep_rank")}
    engine._enqueue_notification = lambda s, nt, ev, pid, data: captured.__setitem__(nt, data)
    engine._loot_sweep_score = lambda s, t, tid, include=None, exclude_id=None: (
        curr_b if include is not None else prev_b)
    engine._current_leader = lambda s, eid, strict=False: None
    engine._task_contributors = lambda s, tid, team_id: [
        {"player_name": "Zezima", "quantity": 12}]
    engine._award_contribution_points = lambda *a, **k: None
    engine._publish = lambda *a, **k: None
    engine._loot_sweep_rank = lambda s, eid, tid: (1, 4)

    # team_channel_interest -> False (no per-team opt-in) and the fake ORM
    # models — all saved/restored so nothing leaks into other suites (which
    # rely on the conftest MagicMock for db.models.EventTeam etc.).
    etd = sys.modules.get("services.event_team_discord")
    etd_saved = getattr(etd, "team_channel_interest", None) if etd else None
    if etd is None:
        import types as _t
        etd = _t.ModuleType("services.event_team_discord")
        sys.modules["services.event_team_discord"] = etd
    etd.team_channel_interest = lambda *a, **k: False
    dbm = sys.modules["db.models"]
    models_saved = (dbm.EventProgress, dbm.EventTeam)
    dbm.EventProgress, dbm.EventTeam = _Progress, _Team

    team = _Team(id=7, score=100)
    progress = _Progress(progress=prev_b["total"], completed=False, task_id=3, team_id=7)

    class _Sess:
        def query(self, model):
            first = progress if model is _Progress else team
            return SimpleNamespace(
                filter=lambda *a, **k: SimpleNamespace(first=lambda: first))

        def add(self, *a):
            pass

        def flush(self):
            pass

    try:
        engine._apply_loot_sweep(
            _Sess(), None, {"id": 27, "message_config": message_config},
            {"id": 3, "type": "loot_sweep", "label": "Kree'arra", "config": config},
            SimpleNamespace(id=99, team_id=7, player_id=5, matched_target=matched,
                            quantity=1, source_type="drop", proof_url=None),
            player_name="Zezima")
    finally:
        for name, fn in orig.items():
            setattr(engine, name, fn)
        if etd_saved is not None:
            etd.team_channel_interest = etd_saved
        dbm.EventProgress, dbm.EventTeam = models_saved
    return captured


class TestSweepEnqueue:
    def test_item_receipt_muted_by_default(self):
        # Group not complete + item toggle off by default -> nothing posts.
        assert _apply(KREE, {"armadyl helmet": 0}, {"armadyl helmet": 1},
                      "Armadyl helmet") == {}

    def test_item_receipt_when_enabled(self):
        got = _apply(KREE, {"armadyl helmet": 0}, {"armadyl helmet": 1}, "Armadyl helmet",
                     message_config={"toggles": {"event_sweep_item": True}})
        assert set(got) == {"event_sweep_item"}
        d = got["event_sweep_item"]
        assert d["received_points"] == 9
        assert (d["item_scored"], d["item_max"]) == (1, 5)
        assert (d["group_have"], d["group_need"]) == (1, 3)
        assert d["next_receipt_points"] == 7.2
        assert d["team_rank"] == 1 and d["team_count"] == 4

    def test_item_min_points_suppresses_low_value(self):
        assert _apply(KREE, {"armadyl helmet": 0}, {"armadyl helmet": 1}, "Armadyl helmet",
                      message_config={"toggles": {"event_sweep_item": True},
                                      "loot_sweep": {"item_min_points": 10}}) == {}

    def test_group_completion_posts_by_default(self):
        got = _apply(KREE, {"armadyl helmet": 1, "armadyl chestplate": 1},
                     {"armadyl helmet": 1, "armadyl chestplate": 1, "armadyl hilt": 1},
                     "Armadyl hilt")
        assert set(got) == {"event_sweep_group"}
        assert got["event_sweep_group"]["bonus_points"] == 40
        assert got["event_sweep_group"]["completion_n"] == 1
        assert got["event_sweep_group"]["contributors"]

    def test_set_completion_posts_group_and_set(self):
        prev = {**{f"ahrim {c}": 1 for c in "abcd"}, **{f"dharok {c}": 1 for c in "abc"}}
        curr = {**{f"ahrim {c}": 1 for c in "abcd"}, **{f"dharok {c}": 1 for c in "abcd"}}
        got = _apply(_barrows(), prev, curr, "dharok d")
        assert set(got) == {"event_sweep_group", "event_sweep_set"}
        assert got["event_sweep_group"]["bonus_points"] == 4
        assert got["event_sweep_set"]["bonus_points"] == 40
        assert got["event_sweep_set"]["completion_n"] == 1

    def test_group_muted_still_lets_set_through(self):
        prev = {**{f"ahrim {c}": 1 for c in "abcd"}, **{f"dharok {c}": 1 for c in "abc"}}
        curr = {**{f"ahrim {c}": 1 for c in "abcd"}, **{f"dharok {c}": 1 for c in "abcd"}}
        got = _apply(_barrows(), prev, curr, "dharok d",
                     message_config={"toggles": {"event_sweep_group": False}})
        assert set(got) == {"event_sweep_set"}
