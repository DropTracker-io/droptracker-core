"""Task 05 — group-config registry tests, incl. parity with the TS source.

Asserts the Python registry (web_api/config_registry.py) matches the shared TS
registry's ``allConfigKeys()`` (packages/api-types/src/group-config.ts). If the
web repo isn't checked out beside this one, the parity check is skipped.
"""

import os
import re

import pytest

from web_api import config_registry as reg


# Sibling web repo location (see workspace layout).
_TS_REGISTRY = "/store/droptracker/web/packages/api-types/src/group-config.ts"


class TestRegistry:
    def test_seasonal_boards_is_base_not_mirror(self):
        # The intentional edge case: exact match wins over prefix-stripping.
        field = reg.get_config_field("seasonal_boards")
        assert field is not None
        assert field["key"] == "seasonal_boards"
        assert field["type"] == "boolean"

    def test_seasonal_mirror_resolves_to_base(self):
        field = reg.get_config_field("seasonal_notify_pbs")
        assert field is not None and field["key"] == "notify_pbs"

    def test_unknown_key(self):
        assert reg.get_config_field("does_not_exist") is None

    def test_coerce_from_storage_types(self):
        bfield = reg.get_config_field("notify_pbs")
        assert reg.coerce_from_storage(bfield, "1") is True
        assert reg.coerce_from_storage(bfield, "0") is False
        assert reg.coerce_from_storage(bfield, None) is True  # default

        ifield = reg.get_config_field("minimum_value_to_notify")
        assert reg.coerce_from_storage(ifield, "2500000") == 2500000
        assert reg.coerce_from_storage(ifield, None) == 100000  # default

    def test_coerce_from_storage_out_of_range_int_is_default(self):
        # Legacy sentinel: template group seeds number_of_pbs_to_display='0'
        # ("unset"); below min must surface the default, not the raw 0.
        pbs = reg.get_config_field("number_of_pbs_to_display")
        assert reg.coerce_from_storage(pbs, "0") == 5
        assert reg.coerce_from_storage(pbs, "99") == 5  # above max
        assert reg.coerce_from_storage(pbs, "7") == 7  # in range passes through

    def test_coerce_to_storage_validation(self):
        assert reg.coerce_to_storage("notify_pbs", True) == "1"
        assert reg.coerce_to_storage("notify_pbs", False) == "0"
        assert reg.coerce_to_storage("minimum_value_to_notify", 5000) == "5000"
        assert reg.coerce_to_storage("min_ca_tier_to_notify", "ELITE") == "ELITE"
        assert reg.coerce_to_storage("seasonal_notify_pbs", True) == "1"

    def test_coerce_to_storage_rejects_bad(self):
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("notify_pbs", "notabool")
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("min_ca_tier_to_notify", "BOGUS")
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("level_minimum_for_notifications", 200)  # > max 99
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("unknown_key", 1)

    @pytest.mark.skipif(
        not os.path.exists(_TS_REGISTRY), reason="web repo not present for parity check"
    )
    def test_parity_with_ts_registry(self):
        with open(_TS_REGISTRY, "r", encoding="utf-8") as f:
            src = f.read()

        # Isolate the GROUP_CONFIG_FIELDS array, then split into per-field chunks
        # at each `key: "..."` boundary (robust to nested braces in `options`).
        array_match = re.search(
            r"GROUP_CONFIG_FIELDS[^=]*=\s*\[(.*?)\n\];", src, re.DOTALL
        )
        assert array_match, "could not locate GROUP_CONFIG_FIELDS array"
        array_src = array_match.group(1)

        chunks = re.split(r'(?=key:\s*")', array_src)
        base_keys = []
        mirror_keys = []
        for chunk in chunks:
            m = re.search(r'key:\s*"([a-zA-Z0-9_]+)"', chunk)
            if not m:
                continue
            key = m.group(1)
            base_keys.append(key)
            if re.search(r"seasonalMirror:\s*true", chunk):
                mirror_keys.append(key)

        expected = set(base_keys) | {f"seasonal_{k}" for k in mirror_keys}
        got = set(reg.all_config_keys())
        assert got == expected, f"missing={expected - got} extra={got - expected}"
