"""Sourceless clan-broadcast drops: picking the item's sole source.

Jagex prints "from X" only for some sources, so lines like "Burning claw
(2,152,407 coins)." used to land on the "Clan Broadcast" sentinel NPC. These
pin the decision rule in ``db.item_sources.pick_sole_source`` and the cached
wrapper the processor calls.
"""
import importlib
import sys
from types import SimpleNamespace

import data.submissions.clan_broadcast as cb
from db.item_sources import (
    INFER_MIN_OBSERVED,
    observed_source_rows,
    pick_sole_source,
)


# ── decision rule ───────────────────────────────────────────────────────────

def test_single_wiki_source_with_no_observations():
    assert pick_sole_source(["Tormented Demon"], {}) == "Tormented Demon"


def test_single_wiki_source_agreeing_with_observations():
    assert pick_sole_source(["Unsired"], {"Unsired": 380}) == "Unsired"


def test_dominant_observation_beats_a_missing_wiki_table():
    # Broken zombie axe: no wiki rows, every observed drop from one NPC.
    assert pick_sole_source([], {"Armoured zombie": 390}) == "Armoured zombie"


def test_dominant_observation_among_several_wiki_sources():
    # Zenyte shard: two wiki sources, Demonic gorillas in practice.
    assert pick_sole_source(
        ["Demonic gorilla", "Tortured gorilla"], {"Demonic gorilla": 337}
    ) == "Demonic gorilla"


def test_multi_source_item_stays_unattributed():
    # Thread of elidinis: ToA modes split 66/34, a guess is wrong a third of the time.
    assert pick_sole_source(
        ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"],
        {"Tombs of Amascut: Expert Mode": 264, "Tombs of Amascut": 134},
    ) is None


def test_raid_lines_fold_modes_into_the_base_raid():
    # "special loot from a raid: Masori mask": a raid for sure, mode unknown.
    wiki = ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"]
    observed = {"Tombs of Amascut: Expert Mode": 242, "Tombs of Amascut": 158}
    assert pick_sole_source(wiki, observed, fold_raid_modes=True) == "Tombs of Amascut"


def test_folding_answers_with_the_base_raid_even_if_unseen_in_observations():
    wiki = ["Chambers of Xeric", "Chambers of Xeric Challenge Mode"]
    observed = {"Chambers of Xeric Challenge Mode": 94}
    assert pick_sole_source(wiki, observed, fold_raid_modes=True) == "Chambers of Xeric"


def test_folding_still_rejects_two_different_raids():
    observed = {"Chambers of Xeric": 50, "Theatre of Blood": 50}
    assert pick_sole_source([], observed, fold_raid_modes=True) is None


def test_single_wiki_source_contradicted_by_observations_is_rejected():
    observed = {"Somewhere Else": 60, "Wiki Boss": 40}
    assert pick_sole_source(["Wiki Boss"], observed) is None


def test_sparse_observations_defer_to_a_single_wiki_source():
    sparse = {"Somewhere Else": INFER_MIN_OBSERVED - 1}
    assert pick_sole_source(["Wiki Boss"], sparse) == "Wiki Boss"


def test_spelling_variants_count_as_one_source():
    observed = {"The Hueycoatl": 200, "Hueycoatl": 176}
    assert pick_sole_source(["Hueycoatl"], observed) == "The Hueycoatl"


def test_no_evidence_at_all():
    assert pick_sole_source([], {}) is None


# ── observed sources never list the sentinel ────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _ObservedSession:
    def __init__(self, drop_rows, names):
        self._calls = [drop_rows, names]

    def execute(self, *_a, **_k):
        return _Result(self._calls.pop(0))


def test_observed_sources_skip_the_sentinel():
    drops = [(1, p) for p in range(10)] + [(2, p) for p in range(10)]
    s = _ObservedSession(drops, [(1, "Clan Broadcast"), (2, "Zulrah")])
    assert [r[1] for r in observed_source_rows(s, [123])] == ["Zulrah"]


# ── processor wrapper: cached, fail-open ────────────────────────────────────

class _Redis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode()


def _wire(monkeypatch, answer):
    # Patched through sys.modules: conftest stubs the ``db`` package, so an
    # attribute import would hand back a MagicMock child instead.
    item_sources = importlib.import_module("db.item_sources")

    fake = _Redis()
    monkeypatch.setattr(cb.redis_client, "client", fake, raising=False)
    calls = []

    def infer(_s, ids, exclude_npc_ids=(), fold_raid_modes=False):
        calls.append(list(ids))
        if isinstance(answer, Exception):
            raise answer
        return answer

    class _S:
        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(item_sources, "infer_sole_source", infer)
    monkeypatch.setattr(item_sources, "item_ids_for_name", lambda _s, _n: [26372])
    monkeypatch.setitem(sys.modules, "db.models.base", SimpleNamespace(Session=_S))
    return fake, calls


def test_inferred_source_is_cached(monkeypatch):
    _fake, calls = _wire(monkeypatch, "Tormented Demon")
    assert cb._inferred_drop_source("Burning claw") == "Tormented Demon"
    assert cb._inferred_drop_source("Burning claw") == "Tormented Demon"
    assert len(calls) == 1


def test_no_answer_is_cached_too(monkeypatch):
    _fake, calls = _wire(monkeypatch, None)
    assert cb._inferred_drop_source("Dragon platelegs") is None
    assert cb._inferred_drop_source("Dragon platelegs") is None
    assert len(calls) == 1


def test_inference_failure_falls_back_to_sentinel(monkeypatch):
    fake, _calls = _wire(monkeypatch, RuntimeError("db down"))
    assert cb._inferred_drop_source("Burning claw") is None
    assert fake.store == {}  # a failure is not cached as "no source"
