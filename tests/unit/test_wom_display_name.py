"""WOM identity lookups must hand back the *display* name, not the username.

The wom library documents ``Player.username`` as "always lowercase";
``check_user_by_username``/``check_user_by_id`` returned it as the second
element of their tuple, and db/ops.create_player and
data/submissions/common.ensure_player_and_auth bind that to ``canonical_name``
and write it to ``Player.player_name``. So every row those paths created had
its capitalisation stripped — 11,940 of 22,267 rows on 2026-08-30 — while the
docstrings claimed all along that the value was the displayName.

The two never differ by anything but case (checked against the live WOM API for
a sample of these accounts: 0 display-equivalence violations), which is what
makes the swap safe for every caller that compares through
normalize_player_display_equivalence.

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
def wom_display_name():
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
        mod = _load_real_module("_real_wiseoldman_display", "utils/wiseoldman.py")
        yield mod._wom_display_name
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_real_wiseoldman_display", None)




def _player(**kwargs):
    return types.SimpleNamespace(**kwargs)


class TestWomDisplayName:
    def test_prefers_display_name_over_lowercase_username(self, wom_display_name):
        assert wom_display_name(_player(username="oakforgedxx", display_name="OakForgedXX")) == "OakForgedXX"

    def test_returns_display_name_with_spaces_intact(self, wom_display_name):
        assert wom_display_name(_player(username="white apron", display_name="White Apron")) == "White Apron"

    def test_falls_back_to_username_when_display_name_is_missing(self, wom_display_name):
        assert wom_display_name(_player(username="zezima")) == "zezima"

    def test_falls_back_when_display_name_is_none(self, wom_display_name):
        assert wom_display_name(_player(username="zezima", display_name=None)) == "zezima"

    def test_falls_back_when_display_name_is_blank(self, wom_display_name):
        # Never let a whitespace-only name become the stored RSN.
        assert wom_display_name(_player(username="zezima", display_name="   ")) == "zezima"

    def test_returns_none_when_the_record_carries_no_name_at_all(self, wom_display_name):
        assert wom_display_name(_player()) is None

    def test_result_is_a_string_not_a_library_object(self, wom_display_name):
        class _Weird(str):
            pass
        assert type(wom_display_name(_player(username="x", display_name=_Weird("Xy")))) is str

    def test_genuinely_lowercase_names_are_passed_through(self, wom_display_name):
        # Some real accounts have no capitals on the hiscores either; that is
        # the true display name and must not be "corrected".
        assert wom_display_name(_player(username="kowalamoon", display_name="kowalamoon")) == "kowalamoon"
