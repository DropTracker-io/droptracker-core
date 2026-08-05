"""Task 12 — superadmin data-viewer registry + comped-grant tests.

The global stubs in tests/conftest.py replace ``db`` with a MagicMock, so the
registry imports resolve to mock model classes. These tests exercise the pure
whitelist / editable-field validation and the comped-grant computation without a
live DB.
"""

from datetime import datetime, timedelta

import pytest

from web_api import admin_registry as reg
from web_api.common import ProblemException


# ── Entity whitelist ──────────────────────────────────────────────────────────

class TestEntityWhitelist:
    def test_required_entities_present(self):
        entities = set(reg.list_entities())
        required = {
            "players", "groups", "users", "group_configurations",
            "subscription_tiers", "group_subscriptions", "audit_log",
            "announcements", "notification_queue", "discord_outbox",
        }
        assert required.issubset(entities)

    def test_unknown_entity_aborts_404(self):
        with pytest.raises(ProblemException) as exc:
            reg.get_spec("drops")  # not whitelisted
        assert exc.value.status == 404

    def test_specs_are_well_formed(self):
        for name in reg.list_entities():
            spec = reg.get_spec(name)
            assert spec["pk"] in spec["columns"], f"{name}: pk not in columns"
            # Editable columns must be a subset of viewable columns.
            assert set(spec["editable"]).issubset(set(spec["columns"])), name
            # Primary keys are never editable.
            assert spec["pk"] not in spec["editable"], f"{name}: pk is editable"

    def test_sensitive_columns_not_exposed(self):
        # users.auth_token is a credential and must never be viewable/editable.
        users = reg.get_spec("users")
        assert "auth_token" not in users["columns"]
        assert "auth_token" not in users["editable"]

    def test_audit_log_is_read_only(self):
        assert reg.get_spec("audit_log")["editable"] == []

    def test_role_flags_not_editable(self):
        # Role grants must go through /admin/users/{id}/{superadmin,developer}
        # (audited, self-revoke-guarded, badge-synced) — never the data editor.
        users = reg.get_spec("users")
        assert "is_superadmin" not in users["editable"]
        assert "is_developer" not in users["editable"]

    def test_developer_readable_is_a_tight_allowlist(self):
        # Developers may only READ entities explicitly flagged; everything
        # carrying PII, billing identifiers or message content stays off.
        readable = {
            name for name in reg.list_entities()
            if reg.get_spec(name).get("developer_readable")
        }
        assert readable == {"players", "groups"}


# ── Editable-field validation ─────────────────────────────────────────────────

class TestEditableValidation:
    def test_accepts_allowlisted_fields(self):
        out = reg.validate_editable_fields("players", {"player_name": "Zezima", "hidden": True})
        assert out == {"player_name": "Zezima", "hidden": True}

    def test_rejects_unknown_entity(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("nope", {"x": 1})
        assert exc.value.status == 404

    def test_rejects_non_editable_field(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("players", {"wom_id": 5})
        assert exc.value.status == 422

    def test_rejects_primary_key_edit(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("players", {"player_id": 9})
        assert exc.value.status == 422

    def test_rejects_empty_body(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("players", {})
        assert exc.value.status == 422

    def test_rejects_non_object_body(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("players", "not-a-dict")
        assert exc.value.status == 422

    def test_read_only_entity_rejects_all(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("audit_log", {"action": "x"})
        assert exc.value.status == 422

    def test_mixed_valid_and_invalid_rejected(self):
        with pytest.raises(ProblemException) as exc:
            reg.validate_editable_fields("users", {"hidden": True, "discord_id": "1"})
        assert exc.value.status == 422


# ── Primary-key coercion ──────────────────────────────────────────────────────

class TestPkCoercion:
    def test_int_pk(self):
        assert reg.coerce_pk(reg.get_spec("players"), "42") == 42

    def test_str_pk(self):
        assert reg.coerce_pk(reg.get_spec("subscription_tiers"), "gold") == "gold"

    def test_bad_int_pk_aborts(self):
        with pytest.raises(ProblemException) as exc:
            reg.coerce_pk(reg.get_spec("players"), "abc")
        assert exc.value.status == 422


# ── Value serialization ───────────────────────────────────────────────────────

class TestSerialize:
    def test_datetime_to_unix(self):
        dt = datetime(2026, 1, 1, 0, 0, 0)
        assert reg.serialize_value(dt) == int(dt.timestamp())

    def test_scalars_passthrough(self):
        assert reg.serialize_value(None) is None
        assert reg.serialize_value(True) is True
        assert reg.serialize_value(7) == 7
        assert reg.serialize_value("x") == "x"

    def test_bytes_decoded(self):
        assert reg.serialize_value(b"abc") == "abc"


# ── Comped grant logic ────────────────────────────────────────────────────────

class TestCompedGrant:
    def test_valid_grant_fields(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        out = reg.build_comped_grant("gold", 30, now=now)
        assert out["provider"] == "manual"
        assert out["status"] == "active"
        assert out["tier_key"] == "gold"
        assert out["cancel_at_period_end"] is False
        assert out["current_period_end"] == now + timedelta(days=30)

    def test_missing_tier_key(self):
        with pytest.raises(ProblemException) as exc:
            reg.build_comped_grant("", 30)
        assert exc.value.status == 422

    def test_non_string_tier_key(self):
        with pytest.raises(ProblemException) as exc:
            reg.build_comped_grant(123, 30)
        assert exc.value.status == 422

    @pytest.mark.parametrize("days", [0, -1, 3651, "30", 1.5, True])
    def test_invalid_days(self, days):
        with pytest.raises(ProblemException) as exc:
            reg.build_comped_grant("gold", days)
        assert exc.value.status == 422

    def test_boundary_days_accepted(self):
        assert reg.build_comped_grant("gold", 1) is not None
        assert reg.build_comped_grant("gold", 3650) is not None
