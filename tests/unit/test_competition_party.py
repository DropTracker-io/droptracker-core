"""Group-content BotW races (suggestions #186 / #187): the ``party`` gate
("a kill only counts with clanmates in it") and the ``party`` / ``learner``
kill rules — the pure scorer pieces in services/competition.py, the engine's
matcher + envelope path, and the write-time validator.

The engine is loaded with the REAL services.competition injected past the
conftest ``services`` stub (the test_competition_engine recipe), and the I/O
edges (``record_match``, the clanmate lookup, the learner KC lookup) are
monkeypatched, so no DB is needed.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


comp = _load("services.competition", os.path.join("services", "competition.py"))
if "services" in sys.modules:
    setattr(sys.modules["services"], "competition", comp)
engine = _load("_competition_party_engine_ut", os.path.join("services", "event_engine.py"))
roster = _load("_clan_roster_ut", os.path.join("utils", "clan_roster.py"))


def _config(**over):
    raw = {
        "kind": "competition", "metric_kind": "boss",
        "npcs": ["Theatre of Blood", "Theatre of Blood: Hard Mode"],
        "format": "individual",
        "ranking": {"mode": "points", "gained_per_point": 1},
        "party": {"require": "any", "mates": "clan", "min_mates": 1},
        "bonus_rules": [],
    }
    raw.update(over)
    return raw


# ── scorer: config ───────────────────────────────────────────────────────────

class TestPartyConfig:
    def test_gate_parsed(self):
        cfg = comp.CompetitionConfig(_config())
        assert cfg.party == {"require": "any", "mates": "clan", "min_mates": 1}
        assert cfg.party_gated is True
        assert cfg.matcher_index()["party"]["require"] == "any"

    def test_no_party_block_is_ungated(self):
        cfg = comp.CompetitionConfig(_config(party=None))
        assert cfg.party is None and cfg.party_gated is False
        assert cfg.matcher_index()["party"] is None

    def test_off_keeps_the_mate_scope_without_gating(self):
        cfg = comp.CompetitionConfig(_config(party={"require": "off"}))
        assert cfg.party["require"] == "off"
        assert cfg.party_gated is False

    def test_team_scope_needs_a_team_race(self):
        cfg = comp.CompetitionConfig(_config(party={"mates": "team"}))
        assert cfg.party["mates"] == "clan"
        cfg = comp.CompetitionConfig(_config(party={"mates": "team"}, format="teams"))
        assert cfg.party["mates"] == "team"

    def test_skill_race_has_no_party(self):
        cfg = comp.CompetitionConfig({"metric_kind": "skill", "skill": "mining",
                                      "party": {"require": "any"}})
        assert cfg.party is None

    def test_kill_rules_only_on_boss_races(self):
        rules = [{"id": 1, "type": "party", "points": 1},
                 {"id": 2, "type": "learner", "points": 4, "max_kc": 50}]
        boss = comp.CompetitionConfig(_config(bonus_rules=rules))
        assert [r.type for r in boss.kill_rules] == ["party", "learner"]
        assert boss.matcher_index()["kill_rules"][1] == {
            "id": 2, "type": "learner", "max_kc": 50}
        skill = comp.CompetitionConfig({"metric_kind": "skill", "skill": "mining",
                                        "bonus_rules": rules})
        assert skill.bonus_rules == ()

    def test_labels(self):
        rules = [{"id": 1, "type": "party"}, {"id": 2, "type": "learner"}]
        cfg = comp.CompetitionConfig(_config(bonus_rules=rules))
        assert comp.rule_label(cfg.rules_by_id[1]) == "Each clanmate in the kill"
        assert comp.rule_label(cfg.rules_by_id[2]) == "Kill with a learner (under 100 KC)"
        team = comp.CompetitionConfig(_config(bonus_rules=rules, format="teams",
                                              party={"mates": "team"}))
        assert comp.rule_label(team.rules_by_id[1]) == "Each teammate in the kill"


# ── scorer: the verdict ──────────────────────────────────────────────────────

ANY = {"require": "any", "mates": "clan", "min_mates": 1}
ALL = {"require": "all", "mates": "clan", "min_mates": 1}


class TestPartyVerdict:
    def test_others_drop_receiver_and_fold_names(self):
        others = comp.party_others(
            ["Some_Guy", "some guy", "Me", "Other Name", "", None], "me")
        assert others == ["some guy", "other name"]

    def test_no_gate_always_counts(self):
        assert comp.party_verdict(None, [], set()) == (True, 0)
        assert comp.party_verdict({"require": "off"}, ["a"], {"a"}) == (True, 1)

    def test_solo_kill_refused(self):
        assert comp.party_verdict(ANY, [], set()) == (False, 0)

    def test_any_needs_min_mates(self):
        assert comp.party_verdict(ANY, ["a", "b"], {"a"}) == (True, 1)
        two = dict(ANY, min_mates=2)
        assert comp.party_verdict(two, ["a", "b"], {"a"}) == (False, 1)
        assert comp.party_verdict(two, ["a", "b"], {"a", "b"}) == (True, 2)

    def test_all_refuses_a_non_mate(self):
        assert comp.party_verdict(ALL, ["a", "b"], {"a"}) == (False, 1)
        assert comp.party_verdict(ALL, ["a", "b"], {"a", "b"}) == (True, 2)

    def test_all_refuses_a_raider_we_cannot_see(self):
        # 4-player raid, only 2 others named: the third can't be shown to be
        # a clanmate.
        assert comp.party_verdict(ALL, ["a", "b"], {"a", "b"}, 4) == (False, 2)
        assert comp.party_verdict(ALL, ["a", "b"], {"a", "b"}, 3) == (True, 2)
        # ``any`` doesn't care who else was there.
        assert comp.party_verdict(ANY, ["a"], {"a"}, 5) == (True, 1)

    def test_learner(self):
        assert comp.has_learner([250, 99], 100) is True
        assert comp.has_learner([100, 250], 100) is False
        assert comp.has_learner([], 100) is False
        assert comp.has_learner([None], 100) is False

    def test_units(self):
        assert comp.kill_rule_units("party", 3, False) == 3
        assert comp.kill_rule_units("learner", 3, True) == 1
        assert comp.kill_rule_units("learner", 3, False) == 0

    def test_roster_key_matches_party_key(self):
        for name in ("Some_Guy", "SOME-guy", "  some  guy ", None, "x"):
            assert roster.roster_name_key(name) == comp.party_name_key(name)


# ── scorer: the fold ─────────────────────────────────────────────────────────

class _Row:
    _next = 0

    def __init__(self, player_id, quantity, note=None):
        _Row._next += 1
        self.id = _Row._next
        self.player_id = player_id
        self.quantity = quantity
        self.note = note
        self.created_at = None
        self.matched_target = "Theatre of Blood"


class TestKillRuleFold:
    def test_add_pays_per_mate(self):
        cfg = comp.CompetitionConfig(_config(bonus_rules=[
            {"id": 1, "type": "party", "points": 2, "unlimited": True}]))
        rows = [_Row(5, 1), _Row(5, 3, "bonus:party:1"),
                _Row(5, 1), _Row(5, 2, "bonus:party:1")]
        per = comp.fold_rows(rows, cfg)[5]
        assert per["gained"] == 2
        assert per["bonus"][1]["awarded"] == 2
        assert per["bonus"][1]["points"] == 10          # (3 + 2) mates × 2
        assert comp.player_points(per, cfg) == 12

    def test_multiply_floors_on_the_total(self):
        cfg = comp.CompetitionConfig(_config(bonus_rules=[
            {"id": 1, "type": "party", "scaling": "multiply", "bonus_pct": 50,
             "unlimited": True}]))
        rows = [_Row(5, 1, "bonus:party:1") for _ in range(3)]
        # 3 kills × 1 mate × 50% of a 1-point kill = 1.5 → 1
        assert comp.fold_rows(rows, cfg)[5]["bonus_points"] == 1
        rows.append(_Row(5, 1, "bonus:party:1"))
        assert comp.fold_rows(rows, cfg)[5]["bonus_points"] == 2

    def test_multiply_scales_with_the_kill_price(self):
        # 1 point per 2 kills: a kill is worth half a point.
        cfg = comp.CompetitionConfig(_config(
            ranking={"mode": "points", "gained_per_point": 2},
            bonus_rules=[{"id": 1, "type": "learner", "scaling": "multiply",
                          "bonus_pct": 400, "unlimited": True}]))
        rows = [_Row(5, 1, "bonus:learner:1") for _ in range(3)]
        assert comp.fold_rows(rows, cfg)[5]["bonus_points"] == 6   # 3 × 4 × ½

    def test_cap_counts_kills(self):
        cfg = comp.CompetitionConfig(_config(bonus_rules=[
            {"id": 1, "type": "party", "points": 1, "max_awards": 2}]))
        rows = [_Row(5, 4, "bonus:party:1") for _ in range(3)]
        slot = comp.fold_rows(rows, cfg)[5]["bonus"][1]
        assert slot["awarded"] == 2 and slot["points"] == 8

    def test_deleted_rule_pays_nothing(self):
        cfg = comp.CompetitionConfig(_config())
        rows = [_Row(5, 4, "bonus:party:9")]
        per = comp.fold_rows(rows, cfg)[5]
        assert per["gained"] == 0 and per["bonus_points"] == 0


# ── engine: matcher ──────────────────────────────────────────────────────────

def _task(config, task_id=10):
    cfg = comp.CompetitionConfig(config)
    return {
        "id": task_id, "type": "competition", "label": "Race",
        "target": cfg.npcs[0], "target_value": 0, "points": 0,
        "config": config, "competition": cfg.matcher_index(),
        "kc_npcs": list(cfg.npcs),
        "wom_metrics": ({} if cfg.party_gated
                        else {"theatre_of_blood": "theatre of blood"}),
        "requires_confirmation": False, "event_id": 1,
    }


def _env(kind, guid="g1", player_id=5, **data):
    return {"v": 1, "kind": kind, "guid": guid, "player_id": player_id,
            "player_name": "Me", "used_api": True, "data": data}


class TestMatcher:
    def test_gated_drop_counts_per_kill(self):
        m = engine.match_task(_task(_config()), _env(
            "drop", npc_name="Theatre of Blood", kill_count=10))
        assert m["mode"] == "kc_kill"

    def test_ungated_drop_keeps_the_watermark(self):
        m = engine.match_task(_task(_config(party=None)), _env(
            "drop", npc_name="Theatre of Blood", kill_count=10))
        assert m["mode"] == "kc"

    def test_gated_race_refuses_wom(self):
        env = _env("wom_kc", boss_metric="theatre_of_blood", kc=50)
        assert engine.match_task(_task(_config()), env) is None
        assert engine.match_task(_task(_config(party=None)), env) is not None

    def test_gated_helper(self):
        assert engine._competition_party_gated(_task(_config())) is True
        assert engine._competition_party_gated(
            _task(_config(party={"require": "off"}))) is False
        assert engine._competition_party_gated({"type": "kc_target"}) is False


# ── engine: envelope path ────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.sets, self.kv = {}, {}

    def sadd(self, key, member):
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def expire(self, key, ttl):
        return True

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def exists(self, key):
        return 1 if key in self.kv else 0

    def delete(self, key):
        self.kv.pop(key, None)


def _event():
    return {"id": 1, "name": "Sanguine Sundays", "group_id": 14,
            "kind": "botw", "requires_confirmation": False,
            "submission_policy": "all", "has_bingo": False, "board_size": 5,
            "bonus_line_points": 0, "bonus_blackout_points": 0,
            "window_start": None, "window_end": None}


@pytest.fixture
def harness(monkeypatch):
    """handle_envelope with the I/O edges replaced: ``recorded`` collects
    every ledger insert as (quantity, bonus type), ``clan`` is the set of
    clanmate name keys and ``kcs`` the learner lookup's answer."""
    recorded, clan, kcs = [], {"mate one", "mate two"}, []

    def fake_record(session, redis_conn, event, task, team_id, player_id,
                    quantity, envelope, cells=None, matched_target=None,
                    path_idx=None, bonus=None):
        recorded.append((envelope["kind"], quantity, (bonus or {}).get("type")))
        return {"kind": "competition"}

    monkeypatch.setattr(engine, "record_match", fake_record)
    monkeypatch.setattr(engine, "_party_mate_keys",
                        lambda session, state, event, team_id, party, others:
                        {k for k in others if k in clan})
    monkeypatch.setattr(engine, "_kill_learner_kcs",
                        lambda session, event, envelope, others: list(kcs))
    redis = _FakeRedis()

    def run(config, envelope):
        state = engine.MatcherState(
            events={1: _event()},
            tasks_by_event={1: [_task(config)]},
            participants={5: [(1, 77, None)]},
        )
        engine.handle_envelope(None, redis, state, envelope)

    return run, recorded, kcs


def _raid(guid="g1", kc=10, party=("Mate One",), item="Coins", **extra):
    return _env("drop", guid=guid, npc_name="Theatre of Blood", npc_id=8359,
                item_name=item, kill_count=kc, party=list(party), **extra)


class TestEnvelopePath:
    def test_solo_kill_earns_nothing(self, harness):
        run, recorded, _ = harness
        run(_config(), _raid(party=()))
        assert recorded == []

    def test_pug_kill_earns_nothing(self, harness):
        run, recorded, _ = harness
        run(_config(), _raid(party=("Random Pug",)))
        assert recorded == []

    def test_clan_kill_counts_once_per_kill(self, harness):
        run, recorded, _ = harness
        run(_config(), _raid(guid="a", item="Coins"))
        run(_config(), _raid(guid="b", item="Cabbage"))   # same raid, 2nd item
        run(_config(), _raid(guid="c", kc=11))            # the next raid
        assert recorded == [("drop", 1, None), ("drop", 1, None)]

    def test_all_mode_refuses_a_pug(self, harness):
        run, recorded, _ = harness
        cfg = _config(party={"require": "all"})
        run(cfg, _raid(party=("Mate One", "Random Pug")))
        assert recorded == []
        run(cfg, _raid(kc=11, party=("Mate One", "Mate Two"), party_size=3))
        assert recorded == [("drop", 1, None)]

    def test_kill_rules_ride_on_the_credited_kill(self, harness):
        run, recorded, kcs = harness
        kcs.extend([250, 40])
        cfg = _config(bonus_rules=[
            {"id": 1, "type": "party", "points": 1, "unlimited": True},
            {"id": 2, "type": "learner", "points": 4, "unlimited": True}])
        run(cfg, _raid(party=("Mate One", "Mate Two", "Random Pug")))
        assert recorded == [("drop", 1, None), ("drop", 2, "party"),
                            ("drop", 1, "learner")]
        # A second item from the same raid adds nothing.
        run(cfg, _raid(guid="g2", party=("Mate One", "Mate Two")))
        assert len(recorded) == 3

    def test_no_learner_no_learner_row(self, harness):
        run, recorded, kcs = harness
        kcs.extend([250, 300])
        cfg = _config(bonus_rules=[{"id": 2, "type": "learner", "points": 4}])
        run(cfg, _raid())
        assert recorded == [("drop", 1, None)]

    def test_ungated_race_pays_mates_but_counts_solo(self, harness):
        run, recorded, _ = harness
        cfg = _config(party={"require": "off"}, bonus_rules=[
            {"id": 1, "type": "party", "points": 1, "unlimited": True}])
        run(cfg, _raid(party=()))
        run(cfg, _raid(guid="g2", kc=11))
        assert recorded == [("drop", 1, None), ("drop", 1, None),
                            ("drop", 1, "party")]

    def test_pet_rides_the_latest_verdict(self, harness):
        run, recorded, _ = harness
        cfg = _config(bonus_rules=[{"id": 3, "type": "pet", "points": 50,
                                    "pets": ["Lil' zik"]}])
        pet = _env("pet", guid="p1", pet_name="Lil' zik", is_new_pet=True)
        run(cfg, pet)                         # no judged kill yet
        assert recorded == []
        run(cfg, _raid(party=()))            # a refused solo raid
        run(cfg, dict(pet, guid="p2"))
        assert recorded == []
        run(cfg, _raid(guid="g2", kc=11))     # a clan raid
        run(cfg, dict(pet, guid="p3"))
        assert recorded == [("drop", 1, None), ("pet", 50, "pet")]


# ── validator ────────────────────────────────────────────────────────────────

etv = pytest.importorskip("web_api.routes.event_task_validation")
from web_api.common import ProblemException  # noqa: E402

RAIDS = {"Theatre of Blood", "Chambers of Xeric", "Nex"}


@pytest.fixture
def botw(monkeypatch):
    by_norm = {n.lower(): n for n in RAIDS}
    monkeypatch.setattr(etv, "_canonical_npc",
                        lambda s, name: by_norm.get((name or "").strip().lower()))
    monkeypatch.setattr(etv, "expand_source_names", lambda name: [name])

    def build(npcs=("Theatre of Blood",), **over):
        body = {"npcs": list(npcs), "ranking": {"mode": "points"},
                "bonus_rules": []}
        body.update(over)
        return etv.validated_competition_config(None, "botw", body)

    return build


class TestValidator:
    def test_default_is_at_least_one_clanmate(self, botw):
        cfg = botw(party={})
        assert cfg["party"] == {"require": "any", "mates": "clan", "min_mates": 1}

    def test_absent_party_stays_absent(self, botw):
        assert "party" not in botw()

    def test_team_mates_need_a_team_race(self, botw):
        with pytest.raises(ProblemException):
            botw(party={"mates": "team"})
        cfg = botw(party={"mates": "team"}, format="teams")
        assert cfg["party"]["mates"] == "team"

    def test_all_is_raids_only(self, botw):
        assert botw(party={"require": "all"})["party"]["require"] == "all"
        with pytest.raises(ProblemException):
            botw(npcs=("Theatre of Blood", "Nex"), party={"require": "all"})
        assert botw(npcs=("Nex",), party={"require": "any"})["party"]

    def test_min_mates_bounds(self, botw):
        with pytest.raises(ProblemException):
            botw(party={"min_mates": 0})
        with pytest.raises(ProblemException):
            botw(party={"min_mates": "2"})
        assert botw(party={"min_mates": 3})["party"]["min_mates"] == 3

    def test_skill_race_refuses_party(self, monkeypatch):
        monkeypatch.setattr(sys.modules["utils.wiseoldman"], "wom_skill_metric",
                            lambda key: key, raising=False)
        with pytest.raises(ProblemException):
            etv.validated_competition_config(
                None, "sotw", {"metric": {"key": "mining"},
                               "party": {"require": "any"}})

    def test_kill_rules(self, botw):
        cfg = botw(bonus_rules=[
            {"type": "party", "points": 2},
            {"type": "party", "scaling": "multiply", "bonus_pct": 50},
            {"type": "learner", "scaling": "multiply", "bonus_pct": 400,
             "max_kc": 50}])
        party_add, party_mul, learner = cfg["bonus_rules"]
        assert party_add["scaling"] == "add" and party_add["points"] == 2
        assert party_mul["bonus_pct"] == 50
        assert learner["max_kc"] == 50 and learner["bonus_pct"] == 400

    def test_kill_rule_rejects(self, botw):
        with pytest.raises(ProblemException):
            botw(bonus_rules=[{"type": "party", "scaling": "double"}])
        with pytest.raises(ProblemException):
            botw(bonus_rules=[{"type": "party", "scaling": "multiply"}])
        with pytest.raises(ProblemException):
            botw(bonus_rules=[{"type": "learner", "max_kc": 0}])
