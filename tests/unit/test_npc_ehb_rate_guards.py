"""The weekly rate recompute must not redefine a completion-marker NPC's rate.

``npc_ehb_rates`` holds one row per NPC, and for a
``services/event_effort.COMPLETION_MARKERS`` NPC that row means something
different from every other row: partial attempts per hour, not kills per hour.
The normal pass measures the latter. If it ever wrote a marker NPC's row, EHE
would silently go back to charging Colosseum bails at the completion rate —
the 329-hour bug — with nothing in the output saying so.

Today the normal pass would skip the Colosseum anyway, because WOM publishes a
``sol_heredit`` rate and priced bosses are skipped. That is incidental
protection: it disappears the moment WOM renames or drops the metric, which is
exactly when nobody is looking. These pin the explicit guard instead.

The script imports ``db`` inside its functions, so the module is loaded by file
path over the conftest stubs like the other engine-adjacent tests.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rates_script():
    spec = importlib.util.spec_from_file_location(
        "_real_compute_npc_ehb_rates",
        REPO_ROOT / "scripts/compute_npc_ehb_rates.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_real_compute_npc_ehb_rates"] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop("_real_compute_npc_ehb_rates", None)


class _Session:
    """Answers only the npc_list lookup ``_marker_npc_ids`` makes."""

    def __init__(self, by_name):
        self.by_name = by_name
        self.queried = []

    def execute(self, _stmt, params=None):
        name = (params or {}).get("n")
        self.queried.append(name)
        npc_id = self.by_name.get(name)
        return types.SimpleNamespace(
            fetchone=lambda: None if npc_id is None else (npc_id,))


class TestMarkerNpcIds:
    def test_markers_resolve_to_their_npc_ids(self, rates_script):
        session = _Session({"fortis colosseum": 13741})
        assert rates_script._marker_npc_ids(session) == {13741}

    def test_lookup_uses_the_normalized_registry_key(self, rates_script):
        # The registry is keyed the way the effort map is (lower-cased), and
        # the query lower-cases npc_name to match. A mismatch here would mean
        # an empty exclusion set and a silently overwritten rate.
        session = _Session({"fortis colosseum": 13741})
        rates_script._marker_npc_ids(session)
        assert session.queried == [q.lower() for q in session.queried]

    def test_an_unknown_marker_npc_is_simply_absent(self, rates_script):
        # No npc_list row yet (a marker added before the NPC is seen) must not
        # raise — it just excludes nothing.
        assert rates_script._marker_npc_ids(_Session({})) == set()

    def test_every_registered_marker_is_covered(self, rates_script):
        from services.event_effort import COMPLETION_MARKERS

        ids = dict(enumerate(COMPLETION_MARKERS, start=1))
        session = _Session({name: i for i, name in ids.items()})
        assert rates_script._marker_npc_ids(session) == set(ids)


class TestPartialRateSanity:
    """A partial is a fraction of a run, so it must be strictly faster."""

    def test_the_measured_colosseum_rate_passes(self, rates_script):
        # 50.2 partial attempts/h against WOM's 2.7 completions/h.
        assert rates_script.partial_rate_is_sane(50.24, 2.7) is True

    def test_a_rate_slower_than_completions_is_refused(self, rates_script):
        # What a stale marker item looks like: every attempt read as a bail,
        # so the "partial" rate converges on the completion cadence.
        assert rates_script.partial_rate_is_sane(2.5, 2.7) is False
        assert rates_script.partial_rate_is_sane(2.7, 2.7) is False

    def test_no_wom_rate_means_nothing_to_check(self, rates_script):
        assert rates_script.partial_rate_is_sane(50.24, None) is True
        assert rates_script.partial_rate_is_sane(50.24, 0) is True

    def test_junk_is_refused_rather_than_published(self, rates_script):
        for rate in (0, -1, None, "x"):
            assert rates_script.partial_rate_is_sane(rate, 2.7) is False, rate


class TestNormalPassExclusion:
    def test_the_guard_does_not_depend_on_wom_pricing_the_metric(self):
        """Regression in intent: the exclusion must be by marker registry, not
        by 'WOM already prices this'.

        If someone reworks the pass, the cheap thing to write is
        ``if wom: continue`` and rely on sol_heredit being priced. This asserts
        the source reads the marker set — the guard that still holds when WOM
        changes its metric names.
        """
        source = (REPO_ROOT / "scripts/compute_npc_ehb_rates.py").read_text()
        body = source.split("def main(", 1)[1]
        assert "_marker_npc_ids(" in body
        marker_line = body.index("if npc_id in marker_ids:")
        wom_line = body.index("wom = wom_rates.get(metric)")
        # The marker check must come FIRST: reaching the wom guard at all means
        # a marker NPC is one WOM rename away from being rewritten.
        assert marker_line < wom_line

    def test_a_normal_run_also_recomputes_partials(self):
        """The weekly timer runs the plain script. If the partial pass only
        happened under --partials it would never recalibrate, and the rate
        would freeze at whatever the content looked like on the day it
        shipped."""
        source = (REPO_ROOT / "scripts/compute_npc_ehb_rates.py").read_text()
        body = source.split("def main(", 1)[1]
        # Once for the --partials-only branch, once in the normal path.
        assert body.count("_run_partials(") >= 2


class TestKcSessionRate:
    """``kc_sessions`` measures fast monsters from the loot kill count.

    ``drop_gaps`` merges kills under 10s apart into one and discards gaps under
    15s, so it can never publish more than 240/h: on 2026-10-08 that priced
    Hobgoblins at 170.6/h against a measured 848/h, inflating EHE ~5x.
    """

    @staticmethod
    def _rows(start_kc, count, every_s, start=None):
        from datetime import datetime, timedelta

        start = start or datetime(2026, 10, 1, 12, 0, 0)
        return [(start_kc + i, start + timedelta(seconds=i * every_s))
                for i in range(count)]

    def test_measures_rates_far_above_the_drop_gaps_ceiling(self, rates_script):
        # One kill every 4s for 20 minutes = 900/h, nearly 4x what drop_gaps
        # can ever report.
        kills, seconds = rates_script.kc_session_rate(self._rows(1, 301, 4))
        assert round(kills * 3600 / seconds) == 900

    def test_a_break_splits_the_session_and_is_not_billed(self, rates_script):
        from datetime import timedelta

        first = self._rows(1, 151, 4)
        later = self._rows(151, 151, 4, start=first[-1][1] + timedelta(hours=2))
        kills, seconds = rates_script.kc_session_rate(first + later)
        assert round(kills * 3600 / seconds) == 900
        assert seconds == 2 * 600

    def test_kills_without_a_reported_drop_still_count(self, rates_script):
        # Every 3rd kill reported: the kc step covers the unreported ones.
        rows = [(kc, t) for kc, t in self._rows(1, 301, 4) if kc % 3 == 1]
        kills, seconds = rates_script.kc_session_rate(rows)
        assert round(kills * 3600 / seconds) == 900

    def test_short_sessions_and_counter_jumps_are_ignored(self, rates_script):
        assert rates_script.kc_session_rate(self._rows(1, 60, 4)) is None
        # A huge kc jump is a different counter, not 5000 kills in 4 seconds.
        rows = self._rows(1, 151, 4)
        rows += self._rows(5000, 151, 4, start=rows[-1][1])
        kills, seconds = rates_script.kc_session_rate(rows)
        assert round(kills * 3600 / seconds) == 900

    def test_no_rows_is_no_rate(self, rates_script):
        assert rates_script.kc_session_rate([]) is None

    def test_ceiling_admits_measured_four_figure_rates(self, rates_script):
        # H.A.M. Member pickpocketing measured 2125/h; 400 dropped it.
        assert rates_script.RATE_MAX_KPH >= 2200


class TestBankedRewards:
    def test_bankable_reward_sources_skip_kc_sessions(self, rates_script):
        for name in ("Reward pool (Tempoross)", "Reward cart (Wintertodt)",
                     "Guardians of the Rift", "Opulent salvage",
                     "Brimstone Chest", "Supply crate (Wintertodt)"):
            assert rates_script.is_banked_reward(name), name

    def test_monsters_are_not_banked_rewards(self, rates_script):
        # "chest" must match as a word: Barrelchest is a boss.
        for name in ("Hobgoblin", "Barrelchest", "H.A.M. Member", None):
            assert not rates_script.is_banked_reward(name), name

    def test_banked_guard_runs_before_kc_sessions(self):
        source = (REPO_ROOT / "scripts/compute_npc_ehb_rates.py").read_text()
        body = source.split("def main(", 1)[1]
        assert "not is_banked_reward(name)" in body
        assert body.index("_kc_sessions_rate(") < body.index("_drop_gaps_rate(")
