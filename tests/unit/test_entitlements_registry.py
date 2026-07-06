"""Entitlements registry parity tests (Task 15)."""

import os
import re

import pytest

from web_api import entitlements_registry as reg


_TS_REGISTRY = "/store/droptracker/web/packages/api-types/src/entitlements.ts"


class TestEntitlementsRegistry:
    def test_resolve_empty_uses_defaults(self):
        assert reg.resolve_tier_entitlements({}) == {
            "events": False,
            "events_max_active": 1,
            "hall_of_fame": False,
            "custom_embeds": False,
        }

    def test_resolve_explicit_defaults(self):
        resolved = reg.resolve_tier_entitlements({"events": True})
        assert resolved["events"] is True
        assert resolved["hall_of_fame"] is False
        assert resolved["events_max_active"] == 1

    def test_validate_rejects_unknown(self):
        with pytest.raises(reg.EntitlementValidationError):
            reg.validate_entitlements_input({"bogus": True})

    def test_int_kind_validation(self):
        assert reg.validate_entitlements_input({"events_max_active": 3}) == {"events_max_active": 3}
        with pytest.raises(reg.EntitlementValidationError):
            reg.validate_entitlements_input({"events_max_active": True})
        with pytest.raises(reg.EntitlementValidationError):
            reg.validate_entitlements_input({"events_max_active": -1})
        with pytest.raises(reg.EntitlementValidationError):
            reg.validate_entitlements_input({"events": 5})

    def test_superadmin_grant_unbounded_ints(self):
        granted = reg.all_entitlements_granted()
        assert granted["events"] is True
        assert granted["events_max_active"] >= 1_000_000

    def test_hof_config_keys(self):
        assert "personal_best_embed_boss_list" in reg.HALL_OF_FAME_CONFIG_KEYS
        assert "hof_individual_boss_messages" in reg.HALL_OF_FAME_CONFIG_KEYS

    @pytest.mark.skipif(not os.path.exists(_TS_REGISTRY), reason="web repo not present")
    def test_parity_with_ts_registry(self):
        with open(_TS_REGISTRY, "r", encoding="utf-8") as f:
            src = f.read()
        keys = re.findall(r'key:\s*"([a-z_]+)"', src)
        # Only keys inside ENTITLEMENT_FIELDS array
        array_match = re.search(r"ENTITLEMENT_FIELDS[^=]*=\s*\[(.*?)\n\];", src, re.DOTALL)
        assert array_match
        array_keys = re.findall(r'key:\s*"([a-z_]+)"', array_match.group(1))
        assert set(reg.all_entitlement_keys()) == set(array_keys)
