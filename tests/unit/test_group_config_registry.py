"""Task 05 — group-config registry tests, incl. parity with the TS source.

Asserts the Python registry (web_api/config_registry.py) matches the shared TS
registry's ``allConfigKeys()`` (packages/api-types/src/group-config.ts). If the
web repo isn't checked out beside this one, the parity check is skipped.
"""

import json
import os
import re

import pytest

from web_api import config_registry as reg


# Sibling web repo location (see workspace layout).
_TS_REGISTRY = "/store/droptracker/web/packages/api-types/src/group-config.ts"


class TestRegistry:
    def test_every_field_has_label_and_category(self):
        # The Discord-native config panel renders straight from this registry,
        # so every field needs human-readable metadata (mirrored from the TS
        # registry — see test_metadata_parity_with_ts_registry below).
        category_keys = {c["key"] for c in reg.CONFIG_CATEGORIES}
        for field in reg.GROUP_CONFIG_FIELDS:
            key = field["key"]
            assert isinstance(field.get("label"), str) and field["label"].strip(), (
                f"{key} has no label"
            )
            assert field.get("category") in category_keys, (
                f"{key} category {field.get('category')!r} not in CONFIG_CATEGORIES"
            )
            # Every field currently ships help text; keep it that way.
            assert isinstance(field.get("help"), str) and field["help"].strip(), (
                f"{key} has no help text"
            )

    def test_config_categories_are_well_formed(self):
        keys = [c["key"] for c in reg.CONFIG_CATEGORIES]
        assert keys, "CONFIG_CATEGORIES must not be empty"
        assert len(keys) == len(set(keys)), "duplicate category keys"
        for cat in reg.CONFIG_CATEGORIES:
            assert cat["label"].strip(), f"category {cat['key']} has no label"

    def test_seasonal_mirror_inherits_base_metadata(self):
        # Mirrors resolve to the base field, so labels/help come along free.
        base = reg.get_config_field("notify_pbs")
        mirror = reg.get_config_field("seasonal_notify_pbs")
        assert mirror is base
        assert mirror["label"] == "Notify personal bests"

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
        # Default matches the runtime fallback in data/submissions/drop.py.
        assert reg.coerce_from_storage(ifield, None) == 2500000  # default

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

    def test_coerce_to_storage_trims_and_bounds_max_length_fields(self):
        # group_name mirrors groups.group_name VARCHAR(30): trailing whitespace
        # must not eat into the limit, and an over-long name is rejected rather
        # than silently truncated by MySQL.
        assert reg.coerce_to_storage("group_name", "  Sailing warriors  ") == "Sailing warriors"
        assert reg.coerce_to_storage("group_name", "x" * 30) == "x" * 30
        assert reg.coerce_to_storage("group_name", "x" * 30 + "  ") == "x" * 30
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("group_name", "x" * 31)

    def test_coerce_to_storage_leaves_unbounded_strings_alone(self):
        # Only fields that declare max_length get the trim treatment.
        assert reg.coerce_to_storage("group_description", " padded ") == " padded "

    def test_coerce_to_storage_messagelist(self):
        # Accepts a JSON string or a real list; stores normalized JSON.
        raw = '["{player_name} has died to {source}!"]'
        assert reg.coerce_to_storage("death_message_variants", raw) == raw
        assert reg.coerce_to_storage(
            "death_message_variants", ["a msg"]) == '["a msg"]'
        # Unset shapes all normalize to "".
        for empty in (None, "", "[]", []):
            assert reg.coerce_to_storage("death_message_variants", empty) == ""

    def test_coerce_to_storage_messagelist_rejects_bad(self):
        for bad in (
            "not json",            # unparseable
            '{"a": 1}',            # not a list
            '[1, 2]',              # non-string entries
            '["  "]',              # blank entry
            '["' + "x" * 201 + '"]',   # entry over the per-message cap
            json.dumps([str(i) for i in range(31)]),  # too many entries
            '["hi @everyone"]',    # content pings for real
            '["hey <@&123>"]',
        ):
            with pytest.raises(reg.ConfigValidationError):
                reg.coerce_to_storage("death_message_variants", bad)

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

    @pytest.mark.skipif(
        not os.path.exists(_TS_REGISTRY), reason="web repo not present for parity check"
    )
    def test_metadata_parity_with_ts_registry(self):
        # label / category / help are copied VERBATIM from the TS registry —
        # the web editor and the Discord config panel must show identical
        # wording. Same chunking approach as the key-parity test above.
        with open(_TS_REGISTRY, "r", encoding="utf-8") as f:
            src = f.read()

        array_match = re.search(
            r"GROUP_CONFIG_FIELDS[^=]*=\s*\[(.*?)\n\];", src, re.DOTALL
        )
        assert array_match, "could not locate GROUP_CONFIG_FIELDS array"

        def grab(chunk, name):
            m = re.search(name + r':\s*"((?:[^"\\]|\\.)*)"', chunk)
            return m.group(1).replace('\\"', '"') if m else None

        ts_meta = {}
        for chunk in re.split(r'(?=key:\s*")', array_match.group(1)):
            m = re.search(r'key:\s*"([a-zA-Z0-9_]+)"', chunk)
            if not m:
                continue
            ts_meta[m.group(1)] = {
                "label": grab(chunk, "label"),
                "category": grab(chunk, "category"),
                "help": grab(chunk, "help"),
            }

        for field in reg.GROUP_CONFIG_FIELDS:
            key = field["key"]
            assert key in ts_meta, f"{key} missing from TS registry"
            for attr in ("label", "category", "help"):
                assert field.get(attr) == ts_meta[key][attr], (
                    f"{key}.{attr} diverged from TS registry: "
                    f"py={field.get(attr)!r} ts={ts_meta[key][attr]!r}"
                )

        # CONFIG_CATEGORIES order + labels mirror the TS constant (TS names
        # the key field `id`).
        ts_cats = re.findall(r'\{\s*id:\s*"(\w+)",\s*label:\s*"([^"]*)"\s*\}', src)
        assert [(c["key"], c["label"]) for c in reg.CONFIG_CATEGORIES] == ts_cats
