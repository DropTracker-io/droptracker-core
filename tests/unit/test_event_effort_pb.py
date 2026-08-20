"""EHE from kill-count and personal-best tasks.

The rule (owner, 2026-07-28): every kill toward a KC task, and every attempt at
a PB task, counts as participating in the event — provided the task isn't done
yet. Two things had to change for that to hold:

* ``pb_target`` tasks were absent from the relevance union entirely, so a boss
  that appears ONLY as a PB task (Theatre of Blood: Hard Mode on the live board
  event) earned nothing however long you spent there.
* effort was gated to an event-kind allowlist that excluded ``board_game``,
  which is why an existing board event's "50 ToB KC" tile looked deliberately
  unrewarded.

Separate file from test_event_effort.py on purpose — that one is being extended
concurrently for derived rates.
"""

import importlib.util
import os
import sys

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_effort_pb_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_effort_pb_test"] = engine
_spec.loader.exec_module(engine)


class _FakeSession:
    """Stands in for the NPC name -> id lookup in _npc_task_descriptors."""

    def __init__(self, npcs):
        self._npcs = npcs

    def query(self, *cols):
        return self

    def all(self):
        return list(self._npcs)


class TestPbTaskRelevance:
    def _descriptors(self, tasks, npcs=(("Theatre of Blood: Hard Mode", 13961),
                                        ("Zulrah", 20))):
        session = _FakeSession([(nid, name) for name, nid in npcs])
        return engine._effort_task_descriptors(session, tasks)

    def test_pb_task_contributes_its_boss(self):
        out = self._descriptors([
            {"id": 198, "type": "pb_target", "target": "Theatre of Blood: Hard Mode"},
        ])
        assert out[0]["npcs"] == ["theatre of blood: hard mode"]
        assert out[0]["npc_ids"]["theatre of blood: hard mode"] == 13961

    def test_pb_task_without_a_target_contributes_nothing(self):
        assert self._descriptors([{"id": 1, "type": "pb_target", "target": None}]) == []

    def test_kc_and_pb_tasks_on_one_boss_both_register(self):
        # The live board event has "50 ToB KC" and "Speedy 3 Man ToB" together;
        # the boss must know about both so it only freezes once BOTH are done.
        out = self._descriptors([
            {"id": 192, "type": "kc_target", "target": "Zulrah"},
            {"id": 201, "type": "pb_target", "target": "Zulrah"},
        ])
        assert [d["task_id"] for d in out] == [192, 201]
        assert all(d["npcs"] == ["zulrah"] for d in out)


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.sets = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def delete(self, key):
        self.kv.pop(key, None)
        self.sets.pop(key, None)

    def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key) or 0) + 1)
        return int(self.kv[key])

    def expire(self, key, ttl):
        return True

    def sadd(self, key, member):
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    def sismember(self, key, member):
        return member in self.sets.get(key, set())


EID, PID = 7, 42
SCOPE = engine._effort_scope("Theatre of Blood")


class TestPbAndDropDoNotDoubleCount:
    """One ToB completion emits BOTH a drop and a kill-time submission. Effort
    must count that as one kill, not two — the same fallback-counter mechanism
    the crediting path uses."""

    def _pb_attempt(self, r, ts):
        """What _apply_effort's pb branch does: cooldown dedupe, then bank a
        provisional +1 in the fallback counter."""
        env = {"ts": ts, "data": {"npc_name": "Theatre of Blood"}}
        if not engine._kc_dedupe(r, EID, SCOPE, PID, env):
            return 0
        r.incr(engine._kc_fallback_key(EID, SCOPE, PID))
        return 1

    def test_pb_then_absolute_kc_nets_one_kill(self):
        r = _FakeRedis()
        banked = self._pb_attempt(r, ts=1000)
        assert banked == 1
        # The drop from that same kill arrives with the real absolute KC.
        folded = engine._fold_kc_watermark(r, EID, SCOPE, PID, 400,
                                           first_credit_offset=1)
        # The +1 already banked by the PB is consumed, not added on top.
        assert banked + folded == 1

    def test_second_kill_still_counts(self):
        r = _FakeRedis()
        self._pb_attempt(r, ts=1000)
        engine._fold_kc_watermark(r, EID, SCOPE, PID, 400, first_credit_offset=1)
        assert engine._fold_kc_watermark(r, EID, SCOPE, PID, 401,
                                         first_credit_offset=1) == 1

    def test_pb_replay_inside_the_cooldown_is_ignored(self):
        r = _FakeRedis()
        assert self._pb_attempt(r, ts=1000) == 1
        assert self._pb_attempt(r, ts=1005) == 0

    def test_a_later_attempt_counts_again(self):
        r = _FakeRedis()
        assert self._pb_attempt(r, ts=1000) == 1
        assert self._pb_attempt(r, ts=1000 + engine.KC_FALLBACK_COOLDOWN_SECONDS + 1) == 1


class TestEventKindNoLongerGates:
    def test_the_event_kind_allowlist_is_gone(self):
        # Relevance is task-type driven now; a board_game event's tiles are
        # ordinary kc/pb/item tasks and must earn EHE like any other.
        assert not hasattr(engine, "EFFORT_EVENT_KINDS")

    def test_resolver_version_rides_in_the_cache_digest(self):
        # Without this, a resolver change (like adding pb_target) keeps serving
        # stale cached maps until someone edits a task.
        tasks = [{"id": 1, "type": "kc_target", "target": "Zulrah", "config": {}}]
        digest = engine._effort_tasks_digest(tasks)
        original = engine._EFFORT_MAP_VERSION
        try:
            engine._EFFORT_MAP_VERSION = f"{original}-bumped"
            assert engine._effort_tasks_digest(tasks) != digest
        finally:
            engine._EFFORT_MAP_VERSION = original


class TestLootSweepRelevance:
    """A loot sweep is an event kind, not an exemption: its sets name bosses
    (Barrows for the brothers' pieces) and its items imply more. Sweeps were
    resolving to an empty map, so a whole event type earned no EHE."""

    def test_group_npcs_are_explicit(self):
        npcs, items = engine._loot_sweep_effort_names({
            "groups": [
                {"label": "Ahrim", "npcs": ["Barrows"],
                 "items": ["Ahrim's hood", "Ahrim's robetop"]},
                {"label": "Dharok", "npcs": ["Barrows"], "items": ["Dharok's helm"]},
            ]
        })
        # Named sources win outright — no inference needed for those items.
        assert npcs == ["barrows"]
        assert items == []

    def test_groups_without_npcs_fall_back_to_their_items(self):
        npcs, items = engine._loot_sweep_effort_names({
            "groups": [{"label": "Open", "items": ["Twisted bow", {"item_name": "Elysian sigil"}]}]
        })
        assert npcs == []
        assert items == ["elysian sigil", "twisted bow"]

    def test_mixed_groups_yield_both(self):
        # A sweep freely mixes NPC-scoped and open groups; one task descriptor
        # has to carry both halves.
        npcs, items = engine._loot_sweep_effort_names({
            "groups": [
                {"npcs": ["Barrows"], "items": ["Ahrim's hood"]},
                {"items": ["Twisted bow"]},
            ]
        })
        assert npcs == ["barrows"] and items == ["twisted bow"]

    def test_v1_flat_config_still_resolves(self):
        npcs, items = engine._loot_sweep_effort_names(
            {"npcs": ["Zulrah"], "items": ["Tanzanite fang"]})
        assert npcs == ["zulrah"] and items == []

    def test_empty_config_is_not_an_error(self):
        assert engine._loot_sweep_effort_names({}) == ([], [])

    def test_loot_sweep_task_reaches_the_descriptors(self):
        session = _FakeSession([(13699, "Barrows")])
        out = engine._effort_task_descriptors(session, [
            {"id": 5, "type": "loot_sweep",
             "config": {"groups": [{"npcs": ["Barrows"], "items": ["Ahrim's hood"]}]}},
        ])
        assert out and out[0]["npcs"] == ["barrows"]


class TestCollectionTaskItemNames:
    """Which names a collection task hands to source-NPC inference. Bug report
    #131: a SINGLE-item task keeps its item in the ``target`` column with no
    config item list at all, so "obtain 7 Sarachnis cudgels" inferred nothing
    and Sarachnis earned no effort all event."""

    def _descriptors(self, tasks):
        return engine._effort_task_descriptors(_FakeSession([]), tasks)

    def test_target_only_task_infers_from_its_target_item(self):
        out = self._descriptors([
            {"id": 591, "type": "item_collection", "target": "Sarachnis cudgel",
             "config": {"progress_notify": "all"}},
        ])
        assert out and out[0]["item_names"] == ["sarachnis cudgel"]

    def test_target_and_config_items_are_unioned(self):
        out = self._descriptors([
            {"id": 1, "type": "item_collection", "target": "Dragon med helm",
             "config": {"items": [{"item_name": "Dragon platelegs"}]}},
        ])
        assert out[0]["item_names"] == ["dragon med helm", "dragon platelegs"]

    def test_pet_list_config_infers_from_the_pets(self):
        out = self._descriptors([
            {"id": 576, "type": "pet_collection", "target": None,
             "config": {"pets": ["Ikkle hydra", "Baron"]}},
        ])
        assert out[0]["item_names"] == ["baron", "ikkle hydra"]

    def test_loot_value_target_is_an_npc_not_an_item(self):
        # The matcher reads a loot_value ``target`` as the NPC the task is
        # scoped to, so it must land in npcs — inferring drop sources FROM it
        # would be nonsense.
        out = self._descriptors([
            {"id": 2, "type": "loot_value", "target": "Vorkath", "config": None},
        ])
        assert out[0]["npcs"] == ["vorkath"] and out[0]["item_names"] == []

    def test_source_restriction_still_wins_over_inference(self):
        out = self._descriptors([
            {"id": 3, "type": "item_collection", "target": "Dragon pickaxe",
             "config": {"source_npcs": ["Callisto"]}},
        ])
        assert out[0]["npcs"] == ["callisto"] and out[0]["item_names"] == []

    def test_task_with_nothing_to_go_on_is_skipped(self):
        assert self._descriptors([
            {"id": 4, "type": "item_collection", "target": None, "config": {}},
        ]) == []
