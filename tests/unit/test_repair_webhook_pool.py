"""Planning logic of scripts/repair_webhook_pool.py — which rows the one-off
pool repair may touch. Restore and purge each need two independent signals
(guild listing + a definitive probe), so neither a Discord incident nor an
incomplete listing can make it delete a live webhook or re-add a dead one.

utils.github is stubbed by conftest, so the real module is swapped in while the
script loads (its verdict constants would otherwise be MagicMocks).
"""
import importlib.util
import os
import sys
from collections import Counter

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules():
    gh = _load("_github_for_repair_tests", "utils", "github.py")
    stub = sys.modules.get("utils.github")
    sys.modules["utils.github"] = gh
    try:
        yield _load("_repair_webhook_pool_under_test", "scripts", "repair_webhook_pool.py"), gh
    finally:
        if stub is None:
            sys.modules.pop("utils.github", None)
        else:
            sys.modules["utils.github"] = stub


def _row(gh, i):
    webhook_id = str(1377677000000000000 + i)
    return gh.PoolRow(i, webhook_id, f"https://discord.com/api/webhooks/{webhook_id}/token-{i}")


class TestPlanDefuse:
    def test_only_rows_without_a_url(self, modules):
        repair, _ = modules
        pending = [
            (2384, "1369498245834997910", None, "1369498243116961924"),
            (7, "1", "", "2"),
            (8, "3", "https://discord.com/api/webhooks/3/token", "4"),
        ]
        assert [row[0] for row in repair.plan_defuse(pending)] == [2384, 7]


class TestPlanRestore:
    def test_needs_absent_from_table_listed_and_alive(self, modules):
        repair, gh = modules
        r1, r2, r3, r4, r5 = (_row(gh, i) for i in range(1, 6))
        snapshot = {r.webhook_id: r.url for r in (r1, r2, r3, r4, r5)}
        listing = {r1.webhook_id: "900855778095800380", r2.webhook_id: "1172737525069135962",
                   r3.webhook_id: "597397938989432842", r5.webhook_id: "900855778095800380"}
        verdicts = {r2.url: gh.ALIVE, r3.url: gh.INCONCLUSIVE, r4.url: gh.ALIVE, r5.url: gh.DEAD}
        restore, skipped = repair.plan_restore(snapshot, [r1], listing, verdicts)
        assert restore == [(r2.webhook_id, r2.url, "core")]
        assert skipped == Counter({"already in table": 1, "not in any guild listing": 1,
                                   "probe inconclusive": 1, "probe dead": 1})

    def test_same_url_under_another_row_counts_as_present(self, modules):
        repair, gh = modules
        r1 = _row(gh, 1)
        other = gh.PoolRow(99, None, r1.url)
        restore, skipped = repair.plan_restore({r1.webhook_id: r1.url}, [other], {r1.webhook_id: "g"},
                                               {r1.url: gh.ALIVE})
        assert restore == [] and skipped == Counter({"already in table": 1})


class TestPlanPurge:
    def test_needs_absent_from_listings_and_unknown_webhook(self, modules):
        repair, gh = modules
        rows = [_row(gh, i) for i in range(1, 5)]
        listing = {rows[0].webhook_id: "900855778095800380"}
        verdicts = {rows[0].url: gh.DEAD,  # listed, so never purged whatever a probe says
                    rows[1].url: gh.DEAD,
                    rows[2].url: gh.INCONCLUSIVE,
                    rows[3].url: gh.ALIVE}
        purge, kept = repair.plan_purge(rows, listing, verdicts)
        assert [row.id for row in purge] == [2]
        assert kept == Counter({gh.INCONCLUSIVE: 1, gh.ALIVE: 1})


class TestPurgeRefusal:
    def test_an_outage_refuses(self, modules):
        repair, _ = modules
        assert "inconclusive" in repair.purge_refusal(600, 400, 10, 1400, 0.5)

    def test_a_mass_purge_refuses(self, modules):
        repair, _ = modules
        assert "more than 50%" in repair.purge_refusal(800, 0, 800, 1400, 0.5)

    def test_a_normal_purge_is_allowed(self, modules):
        repair, _ = modules
        assert repair.purge_refusal(720, 2, 584, 1377, 0.5) is None
