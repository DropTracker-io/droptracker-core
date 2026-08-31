"""``_get_player_metric`` must hand back every field it computed.

The boss branch used to rebuild a fresh ``{"kills": ...}`` on the way out,
throwing away the rank and ehb it had just read off the snapshot — while the
skill, activity and computed branches all returned their dicts whole. Nothing
called it (the KC/rank milestone job reads the bulk-hiscores route instead), so
the loss was invisible; the first caller to ask a boss for its rank would have
got a KeyError for a value that was sitting right there.

Two more bugs fell out of the same read: the computed loop reused
``metric_name`` as its loop variable, clobbering the caller's argument so the
lookup below it could never match, and only the boss branch folded case, so
"Attack" missed the ``attack`` key that "attack" hit.

Like test_wom_degenerate_identity.py, the conftest stubs ``utils.wiseoldman``
with a MagicMock, so the real module is loaded by file path.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_real_module(throwaway_name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(throwaway_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[throwaway_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def get_player_metric():
    """Load the real module over the conftest stubs, then put them back."""
    db_mod = types.ModuleType("db")
    models_ns = types.ModuleType("db.models")
    models_ns.Player = type("Player", (), {})
    models_ns.Group = type("Group", (), {})
    models_ns.NpcList = type("NpcList", (), {})
    db_mod.models = models_ns
    db_mod.NpcList = models_ns.NpcList
    db_mod.Player = models_ns.Player
    db_mod.session = None

    utils_redis_stub = types.ModuleType("utils.redis")

    class _NoopRedis:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    utils_redis_stub.redis_client = _NoopRedis()

    services_ru_stub = types.ModuleType("services.redis_updates")
    services_ru_stub.get_player_list_loot_sum = lambda ids: 0

    saved = {}

    def swap(name, module):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    swap("db", db_mod)
    swap("db.models", models_ns)
    swap("utils.redis", utils_redis_stub)
    swap("services.redis_updates", services_ru_stub)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        mod = _load_real_module("_real_wiseoldman_metric", "utils/wiseoldman.py")
        yield mod._get_player_metric
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_real_wiseoldman_metric", None)


def _call(fn, player_data, metric_name):
    return asyncio.get_event_loop().run_until_complete(fn(player_data, metric_name))


class _Metric:
    """Stands in for wom's ``Metric`` enum: str() gives "Metric.Zulrah"."""

    def __init__(self, name):
        self._name = name

    def __str__(self):
        return f"Metric.{self._name}"

    def __hash__(self):
        return hash(self._name)

    def __eq__(self, other):
        return isinstance(other, _Metric) and other._name == self._name


def _result(*, skills=None, bosses=None, activities=None, computed=None, is_ok=True, **details):
    """Build the ``Result``-shaped object _get_player_metric unwraps."""
    data = None
    if skills is not None or bosses is not None or activities is not None or computed is not None:
        data = types.SimpleNamespace(
            skills={_Metric(k): v for k, v in (skills or {}).items()},
            bosses={_Metric(k): v for k, v in (bosses or {}).items()},
            activities={_Metric(k): v for k, v in (activities or {}).items()},
            computed={_Metric(k): v for k, v in (computed or {}).items()},
        )
    snapshot = types.SimpleNamespace(data=data) if data is not None else None
    details.setdefault("id", 1)
    details.setdefault("username", "some player")
    details.setdefault("display_name", "Some Player")
    unwrapped = types.SimpleNamespace(latest_snapshot=snapshot, **details)
    return types.SimpleNamespace(is_ok=is_ok, unwrap=lambda: unwrapped)


def _boss(kills, rank, ehb):
    return types.SimpleNamespace(kills=kills, rank=rank, ehb=ehb)


class TestBossBranchKeepsEveryField:
    def test_boss_returns_rank_and_ehb_alongside_kills(self, get_player_metric):
        result = _result(bosses={"Zulrah": _boss(kills=1337, rank=4021, ehb=42.5)})
        assert _call(get_player_metric, result, "zulrah") == {
            "kills": 1337,
            "rank": 4021,
            "ehb": 42.5,
        }

    def test_boss_lookup_is_case_and_space_folded(self, get_player_metric):
        # Callers pass display-cased names straight off a submission payload.
        result = _result(bosses={"Chambers_Of_Xeric": _boss(kills=12, rank=900, ehb=3.0)})
        assert _call(get_player_metric, result, "Chambers Of Xeric")["kills"] == 12

    def test_boss_with_no_kills_is_not_reported(self, get_player_metric):
        # Documented: only kills > 0 are collected, so an untouched boss is -1.
        result = _result(bosses={"Zulrah": _boss(kills=0, rank=-1, ehb=0)})
        assert _call(get_player_metric, result, "zulrah") == -1

    def test_non_numeric_kills_does_not_raise(self, get_player_metric):
        result = _result(bosses={"Zulrah": _boss(kills=None, rank=-1, ehb=0)})
        assert _call(get_player_metric, result, "zulrah") == -1


class TestOtherBranchesStillReturnWholeDicts:
    def test_skill_returns_level_experience_rank_ehp(self, get_player_metric):
        skill = types.SimpleNamespace(level=99, experience=13034431, rank=51234, ehp=180.2)
        result = _result(skills={"Attack": skill})
        assert _call(get_player_metric, result, "attack") == {
            "level": 99,
            "experience": 13034431,
            "rank": 51234,
            "ehp": 180.2,
        }

    def test_skill_lookup_folds_case(self, get_player_metric):
        # Before the fix only the boss branch lowered the query, so a
        # capitalised skill name silently fell through to -1.
        skill = types.SimpleNamespace(level=99, experience=13034431, rank=51234, ehp=180.2)
        result = _result(skills={"Attack": skill})
        assert _call(get_player_metric, result, "Attack")["level"] == 99

    def test_activity_returns_score_and_rank(self, get_player_metric):
        activity = types.SimpleNamespace(score=750, rank=2200)
        result = _result(activities={"Clue_Scrolls_All": activity})
        assert _call(get_player_metric, result, "clue_scrolls_all") == {
            "score": 750,
            "rank": 2200,
        }

    def test_computed_returns_value_and_rank(self, get_player_metric):
        # The loop variable used to shadow metric_name, so this lookup compared
        # the last computed key against itself and never matched the caller's.
        # WOM's only computed metrics today are ehp and ehb, and both are also
        # top-level player fields that resolve first (see the precedence test
        # below), so this branch is reached only by a future computed metric.
        result = _result(computed={
            "Some_Future_Metric": types.SimpleNamespace(value=1200.5, rank=8000),
            "Another_One": types.SimpleNamespace(value=430.1, rank=1500),
        })
        assert _call(get_player_metric, result, "some_future_metric") == {
            "value": 1200.5,
            "rank": 8000,
        }

    def test_unknown_metric_is_minus_one_not_the_last_computed_entry(self, get_player_metric):
        # The shadowing bug made the final `in computed_data` check compare a
        # key against the dict it had just been added to, so any miss could
        # hand back the last computed entry instead of -1.
        result = _result(computed={"Some_Future_Metric": types.SimpleNamespace(value=1.0, rank=2)})
        assert _call(get_player_metric, result, "not_a_real_metric") == -1

    def test_ehp_resolves_as_a_top_level_field_not_the_computed_bucket(self, get_player_metric):
        # Precedence is deliberate: player_info is consulted before any
        # snapshot bucket, so "ehp"/"ehb" give the bare float WOM puts on the
        # player record, never the computed {"value", "rank"} dict.
        result = _result(computed={"Ehp": types.SimpleNamespace(value=1200.5, rank=8000)}, ehp=1200.5)
        assert _call(get_player_metric, result, "ehp") == 1200.5


class TestTopLevelFieldsAndDegradedResults:
    def test_top_level_field_returns_the_bare_value(self, get_player_metric):
        result = _result(skills={}, combat_level=126)
        assert _call(get_player_metric, result, "combat_level") == 126

    def test_display_name_is_returned_verbatim(self, get_player_metric):
        result = _result(skills={}, display_name="Zezima")
        assert _call(get_player_metric, result, "display_name") == "Zezima"

    def test_failed_result_is_minus_one(self, get_player_metric):
        assert _call(get_player_metric, _result(is_ok=False), "zulrah") == -1

    def test_player_with_no_snapshot_is_minus_one(self, get_player_metric):
        # A WOM row that has never been scraped has no latest_snapshot at all.
        assert _call(get_player_metric, _result(), "zulrah") == -1

    def test_spaces_and_apostrophes_are_normalised(self, get_player_metric):
        boss = _boss(kills=5, rank=100, ehb=1.0)
        result = _result(bosses={"Kril_Tsutsaroth": boss})
        assert _call(get_player_metric, result, "K'ril Tsutsaroth")["kills"] == 5
