"""resolve_group_entitlements × event rate limits — the web65a fold-in.

A tier whose ``events`` entitlement is off but which carries an enabled
event-rate-limit rule with ``max_events`` > 0 must resolve with
``events: True`` so every consumer (the subscription payload the frontend
gates on, the API gates, the lifecycle sweep) sees the same answer. The
frequency cap itself still binds at activation — that path is covered in
test_event_rate_limits.py.

The conftest loads the REAL ``db.entitlements`` and ``db.event_rate_limits``
by file path, so the lazy ``from db.event_rate_limits import rules_for_tier``
inside the resolver hits the same module instance we pre-warm here.
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

# `import db.entitlements as ent` would bind the stub package's MagicMock
# attribute (PEP 328 binds via getattr on the parent); go to sys.modules,
# where the conftest installed the real path-loaded modules.
ent = sys.modules["db.entitlements"]
erl = sys.modules["db.event_rate_limits"]

GROUP_ID = 42
S = object()  # never touched: pool resolution is patched, rules cache is warm


def _tier(key: str, entitlements: str):
    return SimpleNamespace(key=key, entitlements=entitlements)

FREE = _tier("free", '{"events": false, "events_max_active": 1}')
T3 = _tier("t3", '{"events": true, "events_max_active": 3}')


def _warm(rules: dict) -> None:
    erl._cache["rules"] = rules
    erl._cache["ts"] = time.monotonic()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SITES_BETA_GROUP_IDS", raising=False)
    yield
    erl.invalidate_cache()


def _resolve_with(monkeypatch, tier, pool_tier=None):
    """Resolve entitlements with the pool patched to ``pool_tier`` and the
    free-plan fallback patched to ``tier`` (matching prod: no live legs →
    fallback tier)."""
    monkeypatch.setattr(
        ent, "effective_group_subscription", lambda s, gid: {"tier": pool_tier}
    )
    monkeypatch.setattr(ent, "_load_fallback_tier", lambda s: tier)
    return ent.resolve_group_entitlements(S, GROUP_ID)


class TestRateLimitedGrantFoldIn:
    def test_rule_grants_events_to_free_tier(self, monkeypatch):
        _warm({"free": {"*": {"max_events": 1, "window_days": 365}}})
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is True

    def test_no_rules_keeps_events_off(self, monkeypatch):
        _warm({})
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is False

    def test_rules_for_other_tiers_do_not_grant(self, monkeypatch):
        _warm({"t2": {"*": {"max_events": 1, "window_days": 30}}})
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is False

    def test_zero_max_rule_does_not_grant(self, monkeypatch):
        _warm({"free": {"*": {"max_events": 0, "window_days": 365}}})
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is False

    def test_mixed_rules_any_positive_grants(self, monkeypatch):
        _warm({
            "free": {
                "*": {"max_events": 0, "window_days": 365},
                "bingo": {"max_events": 1, "window_days": 365},
            }
        })
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is True

    def test_entitled_tier_unaffected(self, monkeypatch):
        _warm({})
        resolved = _resolve_with(monkeypatch, T3, pool_tier=T3)
        assert resolved["events"] is True

    def test_other_entitlements_untouched(self, monkeypatch):
        _warm({"free": {"*": {"max_events": 1, "window_days": 365}}})
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["hall_of_fame"] is False
        assert resolved["events_max_active"] == 1

    def test_rules_lookup_error_fails_closed(self, monkeypatch):
        _warm({"free": {"*": {"max_events": 1, "window_days": 365}}})
        monkeypatch.setattr(
            erl, "rules_for_tier", lambda s, key: (_ for _ in ()).throw(RuntimeError)
        )
        resolved = _resolve_with(monkeypatch, FREE)
        assert resolved["events"] is False
