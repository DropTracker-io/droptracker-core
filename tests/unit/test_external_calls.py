"""Third-party API policy for dev instances (utils/external_calls.py).

Both the Wise Old Man and OSRS Wiki maintainers have contacted DropTracker about
request volume, and the wiki blocklisted our User-Agent in 2026-08. A dev box
restored from a production dump is the worst offender available: cold caches,
real player data, and nobody reading the results.

These pin the two properties that matter — production is never affected, and a
dev instance is silent unless someone deliberately said otherwise.
"""

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ec = _load("_external_calls_under_test", "utils", "external_calls.py")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("STATE", "STATUS", "DEV_ALLOW_EXTERNAL_APIS"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestProductionIsUnaffected:
    def test_live_state_allows(self, _clean_env):
        _clean_env.setenv("STATE", "live")
        assert ec.external_apis_allowed() is True

    def test_unset_state_allows(self, _clean_env):
        """A box that never set STATE is production by default, not dev."""
        assert ec.external_apis_allowed() is True

    def test_the_override_is_inert_in_production(self, _clean_env):
        _clean_env.setenv("STATE", "live")
        _clean_env.setenv("DEV_ALLOW_EXTERNAL_APIS", "false")
        assert ec.external_apis_allowed() is True, "must never gag production"


class TestDevIsSilentByDefault:
    @pytest.mark.parametrize("var", ["STATE", "STATUS"])
    def test_dev_blocks(self, _clean_env, var):
        _clean_env.setenv(var, "dev")
        assert ec.external_apis_allowed() is False

    def test_quoted_dev_value_still_counts(self, _clean_env):
        """`.env` in this repo is frequently written STATE="dev"."""
        _clean_env.setenv("STATE", '"dev"')
        assert ec.external_apis_allowed() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", '"true"'])
    def test_explicit_opt_in_allows(self, _clean_env, value):
        _clean_env.setenv("STATE", "dev")
        _clean_env.setenv("DEV_ALLOW_EXTERNAL_APIS", value)
        assert ec.external_apis_allowed() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe", "off"])
    def test_anything_else_stays_blocked(self, _clean_env, value):
        _clean_env.setenv("STATE", "dev")
        _clean_env.setenv("DEV_ALLOW_EXTERNAL_APIS", value)
        assert ec.external_apis_allowed() is False, "only a clear yes counts"


class TestDescribe:
    def test_names_the_override_when_it_is_on(self, _clean_env):
        _clean_env.setenv("STATE", "dev")
        _clean_env.setenv("DEV_ALLOW_EXTERNAL_APIS", "true")
        text = ec.describe()
        assert "ALLOWED" in text and "DEV_ALLOW_EXTERNAL_APIS" in text

    def test_says_blocked_on_a_default_dev_box(self, _clean_env):
        _clean_env.setenv("STATE", "dev")
        assert "blocked" in ec.describe()

    def test_says_production_otherwise(self, _clean_env):
        _clean_env.setenv("STATE", "live")
        assert "production" in ec.describe()
