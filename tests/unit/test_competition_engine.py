"""Engine-integration tests for the SOTW/BOTW (``competition``) wiring in
services/event_engine.py — the matcher branches (``match_task`` /
``match_task_all`` time-tier stacking), the per-player bonus-cap record gate
(``_row_advances_progress``), and the apply/revoke re-folds — driven through
the REAL services/competition scoring (injected past the conftest ``services``
stub, the test_loot_sweep_engine recipe).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_COMP_PATH = os.path.join(_ROOT, "services", "competition.py")
_comp_spec = importlib.util.spec_from_file_location("services.competition", _COMP_PATH)
_comp = importlib.util.module_from_spec(_comp_spec)
sys.modules["services.competition"] = _comp
if "services" in sys.modules:
    setattr(sys.modules["services"], "competition", _comp)
_comp_spec.loader.exec_module(_comp)

_ENGINE_PATH = os.path.join(_ROOT, "services", "event_engine.py")
_spec = importlib.util.spec_from_file_location("_competition_engine_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_competition_engine_ut"] = engine
_spec.loader.exec_module(engine)


BOTW_CONFIG = {
    "kind": "competition",
    "metric_kind": "boss",
    "npcs": ["Zulrah"],
    "ranking": {"mode": "gained"},
    "bonus_rules": [
        {"id": 1, "type": "pet", "points": 100, "max_awards": 1,
         "pets": ["Pet snakeling"]},
        {"id": 2, "type": "time_under", "npc": "Zulrah",
         "threshold_ms": 60_000, "points": 5, "max_awards": 2},
        {"id": 3, "type": "time_under", "npc": "Zulrah",
         "threshold_ms": 50_400, "points": 15, "max_awards": 1},
    ],
}

SOTW_CONFIG = {
    "kind": "competition",
    "metric_kind": "skill",
    "skill": "mining",
    "ranking": {"mode": "points", "gained_per_point": 10_000},
    "bonus_rules": [
        {"id": 1, "type": "pet", "points": 50, "max_awards": 1,
         "pets": ["Rock golem"]},
    ],
}


def _base_task(config, task_id=10):
    """A state-load-shaped task dict (mirrors _task_to_dict's precompute)."""
    cfg = _comp.CompetitionConfig(config)
    return {
        "id": task_id,
        "type": "competition",
        "label": "Race",
        "target": config.get("skill") or (config.get("npcs") or [None])[0],
        "target_value": 0,
        "points": 0,
        "config": config,
        "competition": cfg.matcher_index(),
        "kc_npcs": list(cfg.npcs),
        "wom_metrics": {"zulrah": "zulrah"} if cfg.npcs else {},
        "requires_confirmation": False,
        "event_id": 1,
    }


def _task(config, task_id=10):
    d = _base_task(config, task_id)
    # Mirror _task_to_dict's synthetic-task enrichment for embedded bonus
    # rules. Without it every ``task`` rule matches against a bare dict with
    # no source index and the scoping silently disappears from these tests.
    for rule in (d["competition"].get("task_rules") or ()):
        embedded = rule.get("task")
        if not isinstance(embedded, dict):
            continue
        embedded["id"] = d["id"]
        embedded["event_id"] = d["event_id"]
        embedded.setdefault("points", 0)
        embedded.setdefault("requires_confirmation", False)
        embedded["config"] = engine.parse_task_config(embedded.get("config"))
        engine._enrich_matcher_precompute(embedded)
    return d


def _env(kind, **data):
    return {"v": 1, "kind": kind, "guid": "g1", "player_id": 5, "data": data}


# ── matcher ──────────────────────────────────────────────────────────────────

class TestMatchTask:
    def test_sotw_experience_matches_configured_skill(self):
        task = _task(SOTW_CONFIG)
        m = engine.match_task(task, _env("experience", skill="Mining", xp=1000, level=70))
        assert m == {"mode": "xp", "quantity": 0}
        assert engine.match_task(task, _env("experience", skill="Fishing", xp=1)) is None

    def test_botw_drop_matches_npc_as_kc(self):
        task = _task(BOTW_CONFIG)
        m = engine.match_task(task, _env("drop", npc_name="Zulrah", item_name="X",
                                         kill_count=51))
        # The row names its boss so a multi-boss race can split kills.
        assert m == {"mode": "kc", "quantity": 1, "matched_target": "Zulrah"}
        assert engine.match_task(task, _env("drop", npc_name="Vorkath")) is None

    def test_botw_wom_kc_matches_metric(self):
        task = _task(BOTW_CONFIG)
        m = engine.match_task(task, _env("wom_kc", boss_metric="zulrah", kc=312))
        assert m == {"mode": "kc_abs", "quantity": 0, "matched_target": "Zulrah"}
        assert engine.match_task(task, _env("wom_kc", boss_metric="vorkath")) is None

    def test_metric_kind_gates_envelope_kinds(self):
        assert engine.match_task(_task(SOTW_CONFIG),
                                 _env("drop", npc_name="Zulrah")) is None
        assert engine.match_task(_task(BOTW_CONFIG),
                                 _env("experience", skill="Mining", xp=1)) is None

    def test_pet_bonus_requires_new_and_listed(self):
        task = _task(BOTW_CONFIG)
        m = engine.match_task(task, _env("pet", pet_name="Pet snakeling",
                                         is_new_pet=True))
        assert m["mode"] == "count" and m["quantity"] == 100
        assert m["bonus"] == {"rule_id": 1, "type": "pet"}
        assert m["matched_target"] == "Pet snakeling"
        assert engine.match_task(task, _env("pet", pet_name="Pet snakeling",
                                            is_new_pet=False)) is None
        assert engine.match_task(task, _env("pet", pet_name="Ikkle hydra",
                                            is_new_pet=True)) is None

    def test_pet_bonus_pays_a_duplicate_when_the_rule_says_so(self):
        cfg = json.loads(json.dumps(BOTW_CONFIG))
        for rule in cfg["bonus_rules"]:
            if rule.get("type") == "pet":
                rule["duplicate_pets"] = True
        task = _task(cfg)
        m = engine.match_task(task, _env("pet", pet_name="Pet snakeling",
                                         is_new_pet=False))
        assert m["mode"] == "count" and m["quantity"] == 100
        assert m["bonus"] == {"rule_id": 1, "type": "pet"}
        # Still only the rule's own pets.
        assert engine.match_task(task, _env("pet", pet_name="Ikkle hydra",
                                            is_new_pet=False)) is None

    def test_unconfigured_competition_matches_nothing(self):
        bare = {"id": 1, "type": "competition", "config": {}, "competition": {}}
        assert engine.match_task(bare, _env("drop", npc_name="Zulrah")) is None


class TestMatchTaskAllTimeTiers:
    def test_stacking_tiers_each_award(self):
        task = _task(BOTW_CONFIG)
        # 0:48 — under both 1:00 (rule 2) and 0:50.4 (rule 3).
        matches = engine.match_task_all(
            task, _env("pb", npc_name="Zulrah", time_ms=48_000, team_size="Solo"))
        bonuses = [m["bonus"] for m in matches if m.get("bonus")]
        assert [b["rule_id"] for b in bonuses] == [2, 3]
        assert all(b["type"] == "time_under" for b in bonuses)
        assert [m["quantity"] for m in matches] == [5, 15]

    def test_threshold_edge_inclusive(self):
        task = _task(BOTW_CONFIG)
        matches = engine.match_task_all(
            task, _env("pb", npc_name="Zulrah", time_ms=60_000))
        assert [m["bonus"]["rule_id"] for m in matches] == [2]

    def test_over_threshold_and_wrong_npc_no_match(self):
        task = _task(BOTW_CONFIG)
        assert engine.match_task_all(
            task, _env("pb", npc_name="Zulrah", time_ms=60_600)) == []
        assert engine.match_task_all(
            task, _env("pb", npc_name="Vorkath", time_ms=10_000)) == []

    def test_time_ms_carried_for_note(self):
        task = _task(BOTW_CONFIG)
        matches = engine.match_task_all(
            task, _env("pb", npc_name="Zulrah", time_ms=55_800))
        assert matches[0]["bonus"]["time_ms"] == 55_800


# ── fake session plumbing ────────────────────────────────────────────────────
# query(model) dispatch compares against the conftest db-stub's model
# attributes — MagicMock caches attribute children, so identity holds.


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def with_for_update(self, *a, **k):
        return self

    def limit(self, *a):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Dispatches query(model) on the conftest db-stub identity: ledger rows
    for EventCompletion, the progress row for EventProgress, the team row for
    EventTeam."""

    def __init__(self, ledger=None, progress=None, team=None):
        self.ledger = ledger if ledger is not None else []
        self.progress = progress
        self.team = team
        self.added = []

    def query(self, model, *a):
        from db.models import EventCompletion, EventProgress, EventTeam

        if model is EventCompletion:
            return _Q(self.ledger)
        if model is EventProgress:
            return _Q([self.progress] if self.progress is not None else [])
        if model is EventTeam:
            return _Q([self.team] if self.team is not None else [])
        return _Q([])

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _ledger_row(rid, player_id, qty, note=None, source_type="drop",
                status="auto"):
    return SimpleNamespace(id=rid, player_id=player_id, quantity=qty,
                           note=note, source_type=source_type, status=status,
                           matched_target=None, proof_url=None,
                           created_at=None, team_id=1)


# ── record gate ──────────────────────────────────────────────────────────────

class TestRowAdvances:
    def test_gained_rows_always_advance(self):
        task = _task(BOTW_CONFIG)
        session = _Session(ledger=[])
        candidate = _ledger_row(99, 5, 1)
        assert engine._row_advances_progress(session, task, 1, candidate) is True

    def test_bonus_row_blocked_at_cap(self):
        task = _task(BOTW_CONFIG)  # rule 2 max_awards=2
        existing = [
            _ledger_row(1, 5, 5, note="bonus:time_under:2"),
            _ledger_row(2, 5, 5, note="bonus:time_under:2"),
        ]
        session = _Session(ledger=existing)
        candidate = _ledger_row(99, 5, 5, note="bonus:time_under:2 | 0:55")
        assert engine._row_advances_progress(session, task, 1, candidate) is False
        # Another player is unaffected by player 5's cap.
        other = _ledger_row(99, 6, 5, note="bonus:time_under:2")
        assert engine._row_advances_progress(session, task, 1, other) is True

    def test_bonus_row_advances_below_cap(self):
        task = _task(BOTW_CONFIG)
        session = _Session(ledger=[_ledger_row(1, 5, 5, note="bonus:time_under:2")])
        candidate = _ledger_row(99, 5, 5, note="bonus:time_under:2")
        assert engine._row_advances_progress(session, task, 1, candidate) is True

    def test_unlimited_rule_is_never_blocked(self):
        config = {**BOTW_CONFIG, "bonus_rules": [
            {"id": 2, "type": "time_under", "npc": "Zulrah",
             "threshold_ms": 60_000, "points": 5, "max_awards": 2,
             "unlimited": True}]}
        task = _task(config)
        existing = [_ledger_row(i, 5, 5, note="bonus:time_under:2")
                    for i in range(1, 151)]
        session = _Session(ledger=existing)
        candidate = _ledger_row(999, 5, 5, note="bonus:time_under:2 | 0:55")
        assert engine._row_advances_progress(session, task, 1, candidate) is True


# ── apply / revoke ───────────────────────────────────────────────────────────

class TestApplyRevoke:
    def _fixture(self, config=BOTW_CONFIG):
        progress = SimpleNamespace(progress=0, completed=False)
        team = SimpleNamespace(id=1, score=0)
        session = _Session(ledger=[], progress=progress, team=team)
        event = {"id": 7, "name": "E", "message_config": None}
        task = _task(config)
        return session, event, task, progress, team

    def test_apply_gained_row(self, monkeypatch):
        session, event, task, progress, team = self._fixture()
        frames, enqueued = [], []
        monkeypatch.setattr(engine, "_publish", lambda eid, frame: frames.append(frame))
        monkeypatch.setattr(engine, "_enqueue_notification",
                            lambda *a, **k: enqueued.append(a))
        prior = _ledger_row(1, 6, 10)          # another player, 10 kills
        mine = _ledger_row(2, 5, 3)            # this apply's row, 3 kills
        session.ledger = [prior, mine]
        # The prior row's own apply already folded its score in (delta model).
        team.score = 10
        result = engine._apply_competition(session, None, event, task, mine,
                                           player_name="Alice")
        assert result["kind"] == "competition"
        assert result["gained"] == 3 and result["is_bonus"] is False
        assert result["rank"] == 2 and result["participants"] == 2
        assert result["leader"]["player_id"] == 6
        assert progress.progress == 13 and progress.completed is False
        assert team.score == 13                # gained mode: score = total gained
        assert frames and frames[0]["player_name"] == "Alice"
        assert not enqueued                    # gained rows never notify

    def test_apply_bonus_row_notifies_with_detail(self, monkeypatch):
        session, event, task, progress, team = self._fixture()
        enqueued = []
        monkeypatch.setattr(engine, "_publish", lambda *a: None)
        monkeypatch.setattr(
            engine, "_enqueue_notification",
            lambda s, ntype, ev, pid, payload: enqueued.append((ntype, payload)))
        kill = _ledger_row(1, 5, 1)
        bonus = _ledger_row(2, 5, 5, note="bonus:time_under:2 | 0:55",
                            source_type="pb")
        session.ledger = [kill, bonus]
        team.score = 1                         # the kill's apply already landed
        result = engine._apply_competition(session, None, event, task, bonus,
                                           player_name="Alice")
        assert result["is_bonus"] is True and result["bonus_points"] == 5
        # Gained mode: the team score IS total gained — bonus points show in
        # their own column, never inflating the WOM-parity number.
        assert team.score == 1
        ntype, payload = enqueued[0]
        assert ntype == "event_competition_bonus"
        assert payload["points"] == 5
        assert payload["bonus"]["cap_line"] == "Award 1 of 2"
        assert "0:55" in payload["bonus"]["reason"]
        assert payload["rank_value_text"].endswith("KC")

    def test_revoke_refolds_and_takes_score_back(self, monkeypatch):
        session, event, task, progress, team = self._fixture()
        monkeypatch.setattr(engine, "_publish", lambda *a: None)
        survivor = _ledger_row(1, 5, 10)
        revoked = _ledger_row(2, 5, 4, status="revoked")
        session.ledger = [survivor]            # applied query sees survivors only
        progress.progress = 14
        team.score = 14
        summary = engine._revoke_competition(session, event, task, 1, revoked)
        assert summary["progress"] == 10 and summary["completed"] is False
        assert team.score == 10

    def test_points_mode_score_combines_bonus(self, monkeypatch):
        session, event, task, progress, team = self._fixture(SOTW_CONFIG)
        monkeypatch.setattr(engine, "_publish", lambda *a: None)
        monkeypatch.setattr(engine, "_enqueue_notification", lambda *a, **k: None)
        xp = _ledger_row(1, 5, 25_000, source_type="experience")
        pet = _ledger_row(2, 5, 50, note="bonus:pet:1", source_type="pet")
        session.ledger = [xp, pet]
        team.score = 2                         # the xp row's apply already landed
        result = engine._apply_competition(session, None, event, task, pet,
                                           player_name="Alice")
        # floor(25k/10k)=2 pts + 50 bonus = 52 combined.
        assert result["points"] == 52
        assert team.score == 52
        assert progress.progress == 25_000     # progress stays raw gained


# ── embedded task bonus rules ────────────────────────────────────────────────

SET_BONUS = {
    "id": 4, "type": "task", "points": 25, "max_awards": 1,
    "progress_kind": "distinct", "need": 2,
    "kinds": ["drop", "clog", "pet"],
    "task": {"type": "item_collection", "target": None, "target_value": 2,
             "config": {"kind": "all_of",
                        "items": ["Tanzanite fang", "Magic fang"],
                        "item_npcs": {"Tanzanite fang": ["Zulrah"],
                                      "Magic fang": ["Zulrah"]},
                        "clog_sources": True}},
}
GP_BONUS = {
    "id": 5, "type": "task", "points": 10, "max_awards": 5,
    "progress_kind": "count", "need": 1_000_000, "kinds": ["drop"],
    "task": {"type": "loot_value", "target": "", "target_value": 1_000_000,
             "config": {"source_npcs": ["Zulrah"]}},
}
BOTW_TASK_CONFIG = {**BOTW_CONFIG,
                    "bonus_rules": BOTW_CONFIG["bonus_rules"] + [SET_BONUS, GP_BONUS]}


class TestEmbeddedTaskMatching:
    def test_a_listed_drop_from_the_raced_boss_credits_its_rule(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("drop", npc_name="Zulrah", item_name="Tanzanite fang",
                       quantity=1, total_value=3_000_000))
        by_rule = {(m.get("bonus") or {}).get("rule_id"): m for m in matches}
        # The kill itself still scores the race…
        assert any(m.get("mode") == "kc" for m in matches)
        # …and the same drop advances both bonus rules independently.
        assert by_rule[4]["matched_target"] == "Tanzanite fang"
        assert by_rule[4]["quantity"] == 1
        assert by_rule[5]["quantity"] == 3_000_000

    def test_the_same_item_from_another_boss_credits_nothing(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("drop", npc_name="Vorkath", item_name="Tanzanite fang",
                       quantity=1, total_value=3_000_000))
        # Scoping is enforced by the injected source restriction at MATCH time,
        # so an off-boss drop never even reaches the ledger.
        assert [m for m in matches if m.get("bonus")] == []
        assert matches == []            # not the raced NPC, so no kill either

    def test_a_clog_unlock_at_the_raced_boss_counts(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("clog", npc_name="Zulrah", item_name="Magic fang"))
        bonus = [m for m in matches if (m.get("bonus") or {}).get("rule_id") == 4]
        assert len(bonus) == 1 and bonus[0]["matched_target"] == "Magic fang"

    def test_an_unlisted_item_credits_nothing(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("drop", npc_name="Zulrah", item_name="Ruby",
                       quantity=1, total_value=5000))
        assert [m for m in matches if (m.get("bonus") or {}).get("rule_id") == 4] == []

    def test_the_races_own_kc_watermark_is_untouched_by_bonus_rules(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("drop", npc_name="Zulrah", item_name="Tanzanite fang",
                       quantity=1, total_value=3_000_000))
        # Exactly ONE kc-mode match (the race). A bonus rule that leaked a
        # kc/kc_abs/xp mode would reach the shared watermark folds — which are
        # scoped by task, and the task here IS the race.
        assert len([m for m in matches if m["mode"] in ("kc", "kc_abs", "xp")]) == 1
        assert all(m["mode"] == "count"
                   for m in matches if m.get("bonus"))

    def test_a_kind_prefilter_skips_irrelevant_envelopes(self):
        task = _task(BOTW_TASK_CONFIG)
        matches = engine.match_task_all(
            task, _env("pb", npc_name="Zulrah", time_ms=45_000))
        # pb reaches the two time tiers, never the drop-only rules.
        assert {(m["bonus"]["rule_id"]) for m in matches if m.get("bonus")} == {2, 3}

    def test_an_embedded_kc_task_can_never_reach_the_watermark_folds(self):
        # Belt and braces with the validator's 422: even if a kc_target config
        # were hand-written into a rule, the mode guard drops it.
        smuggled = {
            "id": 6, "type": "task", "points": 5, "max_awards": 1,
            "progress_kind": "count", "need": 1, "kinds": ["drop"],
            "task": {"type": "kc_target", "target": "Zulrah",
                     "target_value": 10, "config": None},
        }
        task = _task({**BOTW_CONFIG, "bonus_rules": [smuggled]})
        matches = engine.match_task_all(
            task, _env("drop", npc_name="Zulrah", item_name="Ruby",
                       quantity=1, total_value=5000))
        assert [m for m in matches if (m.get("bonus") or {}).get("rule_id") == 6] == []


class TestCaTargetMatching:
    def _ca_task(self, **over):
        task = {"id": 11, "type": "ca_target", "target": None,
                "target_value": 3, "points": 0, "config": {
                    "task_names": ["Abyssal Adept", "Abyssal Veteran"]},
                "label": "CAs"}
        task.update(over)
        return engine._enrich_matcher_precompute(task)

    def test_a_listed_achievement_credits(self):
        hit = engine.match_task(self._ca_task(),
                                _env("ca", task_name="Abyssal Adept", tier="Hard"))
        assert hit == {"mode": "count", "quantity": 1,
                       "matched_target": "Abyssal Adept"}

    def test_an_unlisted_achievement_does_not(self):
        assert engine.match_task(
            self._ca_task(), _env("ca", task_name="Noxious Foe", tier="Easy")) is None

    def test_an_empty_allow_list_credits_nothing_rather_than_everything(self):
        # The matcher can't reach the registry, so "no list" must mean "no",
        # never "any combat achievement in the game".
        task = self._ca_task(config={"task_names": []})
        assert engine.match_task(
            task, _env("ca", task_name="Abyssal Adept", tier="Hard")) is None

    def test_a_single_named_target_matches_exactly(self):
        task = self._ca_task(target="Noxious Foe", config={})
        assert engine.match_task(
            task, _env("ca", task_name="noxious foe", tier="Easy")) is not None
        assert engine.match_task(
            task, _env("ca", task_name="Abyssal Adept", tier="Hard")) is None


POOL_BONUS = {
    "id": 7, "type": "task", "points": 10, "max_awards": 5,
    "progress_kind": "points", "need": 500, "kinds": ["drop", "clog", "pet"],
    "task": {"type": "item_collection", "target": None, "target_value": 500,
             "config": {"kind": "point_collection",
                        "items": [{"item_name": "Tanzanite fang", "points": 300},
                                  {"item_name": "Magic fang", "points": 200}],
                        "item_npcs": {"Tanzanite fang": ["Zulrah"],
                                      "Magic fang": ["Zulrah"]},
                        "clog_sources": True}},
}


class TestWeightedPoolEndToEnd:
    """The matcher and the fold have to agree on where the weight is applied —
    if both apply it, a 300-point drop against a 500-point goal folds to 90,000
    and maxes the rule on its first item."""

    def _rows(self, items):
        task = _task({**BOTW_CONFIG, "bonus_rules": [POOL_BONUS]})
        rows, out = [], []
        for n, item in enumerate(items):
            env = {"v": 1, "kind": "drop", "guid": f"g{n}", "player_id": 5,
                   "data": {"npc_name": "Zulrah", "item_name": item,
                            "quantity": 1, "total_value": 1000}}
            for m in engine.match_task_all(task, env):
                if (m.get("bonus") or {}).get("rule_id") != 7:
                    continue
                out.append(m["quantity"])
                rows.append(SimpleNamespace(
                    id=len(rows) + 1, player_id=5, quantity=m["quantity"],
                    note=_comp.bonus_note("task", 7), created_at=len(rows),
                    matched_target=m.get("matched_target"), source_type="drop"))
        return task, rows, out

    def test_the_matcher_supplies_the_weight_once(self):
        _task_dict, _rows, quantities = self._rows(["Tanzanite fang", "Magic fang"])
        assert quantities == [300, 200]

    def test_two_drops_worth_500_pay_exactly_one_award(self):
        task, rows, _q = self._rows(["Tanzanite fang", "Magic fang"])
        cfg = _comp.CompetitionConfig(task["config"])
        per = _comp.fold_rows(rows, cfg)
        slot = per[5]["bonus"][7]
        assert slot["progress"] == 500 and slot["awarded"] == 1
        assert per[5]["bonus_points"] == 10

    def test_one_300_point_drop_does_not_max_the_rule(self):
        task, rows, _q = self._rows(["Tanzanite fang"])
        cfg = _comp.CompetitionConfig(task["config"])
        per = _comp.fold_rows(rows, cfg)
        assert per[5]["bonus"][7]["progress"] == 300
        assert per[5]["bonus_points"] == 0


# ── team races ───────────────────────────────────────────────────────────────

TEAM_BOTW = {**BOTW_CONFIG, "format": "teams"}


def _team_row(rid, player_id, qty, team_id, note=None, source_type="drop"):
    row = _ledger_row(rid, player_id, qty, note=note, source_type=source_type)
    row.team_id = team_id
    return row


class TestTeamRaceApply:
    """The engine's team-race wiring. The fake session can't evaluate SQL
    filters, so the per-team helpers are routed through small fakes that
    honour the team id — what's under test is what the apply does with
    them."""

    def _wire(self, monkeypatch, *, roster=None, rank=(1, 2)):
        scores, leads, frames, enqueued = {}, [], [], []
        monkeypatch.setattr(engine, "_publish", lambda eid, frame: frames.append(frame))
        monkeypatch.setattr(engine, "_enqueue_notification",
                            lambda s, ntype, ev, pid, payload: enqueued.append((ntype, payload)))
        monkeypatch.setattr(engine, "_set_competition_team_score",
                            lambda s, tid, value: scores.__setitem__(tid, value) or value)
        monkeypatch.setattr(engine, "_loot_sweep_rank", lambda s, eid, tid: rank)
        monkeypatch.setattr(engine, "_leader_snapshot", lambda s, ev: ("before",))
        monkeypatch.setattr(
            engine, "_announce_lead_change",
            lambda s, ev, before, pid=None, **k: leads.append((before, pid, k.get("reason"))))
        monkeypatch.setattr(engine, "_competition_roster_ids",
                            lambda s, tid: list((roster or {}).get(tid, [])))
        return scores, leads, frames, enqueued

    def test_a_row_scores_its_own_team_and_ranks_the_player_race_wide(self, monkeypatch):
        scores, leads, frames, _ = self._wire(monkeypatch)
        progress = SimpleNamespace(progress=0, completed=False)
        session = _Session(progress=progress)
        red_a = _team_row(1, 5, 10, team_id=1)
        red_b = _team_row(2, 6, 4, team_id=1)
        blue = _team_row(3, 7, 8, team_id=2)       # this apply's row
        session.ledger = [red_a, red_b, blue]
        event = {"id": 7, "name": "E", "message_config": None}
        task = _task(TEAM_BOTW)
        result = engine._apply_competition(session, None, event, task, blue,
                                           player_name="Cara")
        # Blue's progress and score fold only blue's rows…
        assert progress.progress == 8
        assert scores == {2: 8}
        # …while the player's rank is across the whole race (10 > 8 > 4).
        assert result["rank"] == 2 and result["participants"] == 3
        assert result["leader"]["player_id"] == 5
        assert result["team_rank"] == 1 and result["team_count"] == 2
        assert frames[0]["team_rank"] == 1
        # The overtake check ran, bracketing the write.
        assert leads == [(("before",), 7, "completion")]

    def test_an_individual_race_never_checks_for_a_lead_change(self, monkeypatch):
        _scores, leads, _frames, _ = self._wire(monkeypatch)
        # The real announcer, which returns at once for the no-op snapshot.
        monkeypatch.setattr(engine, "_announce_lead_change",
                            lambda s, ev, before, *a, **k: leads.append(before)
                            if before is not engine._NO_LEAD_SNAPSHOT else None)
        session = _Session(progress=SimpleNamespace(progress=0, completed=False))
        row = _ledger_row(1, 5, 3)
        session.ledger = [row]
        result = engine._apply_competition(session, None, {"id": 7, "name": "E"},
                                           _task(BOTW_CONFIG), row)
        assert leads == [] and "team_rank" not in result

    def test_average_scoring_divides_by_the_roster(self, monkeypatch):
        scores, _leads, _frames, _ = self._wire(monkeypatch, roster={1: [5, 6, 8, 9]})
        session = _Session(progress=SimpleNamespace(progress=0, completed=False))
        a = _team_row(1, 5, 10, team_id=1)
        b = _team_row(2, 6, 5, team_id=1)
        session.ledger = [a, b]
        engine._apply_competition(session, None, {"id": 7, "name": "E"},
                                  _task({**TEAM_BOTW, "team_scoring": "average"}), b)
        assert scores == {1: round(15 / 4, 2)}

    def test_a_bonus_message_carries_the_team_standing(self, monkeypatch):
        _scores, _leads, _frames, enqueued = self._wire(monkeypatch, rank=(2, 3))
        session = _Session(progress=SimpleNamespace(progress=0, completed=False))
        bonus = _team_row(1, 5, 5, team_id=1, note="bonus:time_under:2 | 0:55",
                          source_type="pb")
        session.ledger = [bonus]
        engine._apply_competition(session, None, {"id": 7, "name": "E"},
                                  _task(TEAM_BOTW), bonus, player_name="Alice")
        ntype, payload = enqueued[0]
        assert ntype == "event_competition_bonus"
        assert payload["team_race"] is True
        assert (payload["team_rank"], payload["team_count"]) == (2, 3)

    def test_a_players_bonus_cap_follows_them_across_teams(self):
        # Rule 2 allows two awards. Two were earned on team 1; after moving to
        # team 2 the player must not start a fresh cap.
        task = _task(TEAM_BOTW)
        session = _Session(ledger=[
            _team_row(1, 5, 5, team_id=1, note="bonus:time_under:2"),
            _team_row(2, 5, 5, team_id=1, note="bonus:time_under:2"),
        ])
        candidate = _team_row(99, 5, 5, team_id=2, note="bonus:time_under:2 | 0:51")
        assert engine._row_advances_progress(session, task, 2, candidate) is False


class TestTeamRaceRevoke:
    def test_revoke_rewrites_the_team_score_absolutely(self, monkeypatch):
        monkeypatch.setattr(engine, "_publish", lambda *a: None)
        progress = SimpleNamespace(progress=14, completed=False)
        team = SimpleNamespace(id=1, score=14)
        session = _Session(ledger=[_ledger_row(1, 5, 10)], progress=progress, team=team)
        revoked = _ledger_row(2, 5, 4, status="revoked")
        summary = engine._revoke_competition(session, {"id": 7}, _task(TEAM_BOTW), 1, revoked)
        assert summary["team_score"] == 10 and team.score == 10
        assert progress.progress == 10


class TestTeamScoreWrite:
    def test_writes_only_when_the_value_moved(self):
        team = SimpleNamespace(id=1, score=12)
        session = _Session(team=team)
        assert engine._set_competition_team_score(session, 1, 12) is None
        assert engine._set_competition_team_score(session, 1, 12.5) == 12.5
        assert team.score == 12.5
        assert engine._set_competition_team_score(session, 1, 13.0) == 13
        assert isinstance(team.score, int)
        assert engine._set_competition_team_score(session, None, 5) is None
