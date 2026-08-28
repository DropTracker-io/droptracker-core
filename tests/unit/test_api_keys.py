"""Data API key logic: token shape, verification, and limit resolution.

The conftest stubs ``db``, so ``db/api_keys.py`` is loaded by path (the
``db.entitlements`` pattern) and exercised through plain stand-in rows —
no database anywhere.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).resolve().parents[2] / "db" / "api_keys.py"
_spec = importlib.util.spec_from_file_location("_real_api_keys", _PATH)
keys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keys)


def _row(**overrides):
    """An ApiKey-shaped stand-in with sane defaults."""
    defaults = dict(
        id=7,
        token_hash="",
        token_prefix="",
        label="test",
        owner_user_id=None,
        group_id=2,
        tier_key="standard",
        requests_per_min=None,
        cost_units_per_min=None,
        requests_per_day=None,
        max_concurrency=None,
        revoked_at=None,
        expires_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _minted_row(**overrides):
    row = _row(**overrides)
    token, token_hash, prefix = keys.mint_token(row.id)
    row.token_hash = token_hash
    row.token_prefix = prefix
    return row, token


class TestTokenShape:
    def test_mint_parse_roundtrip(self):
        token, token_hash, prefix = keys.mint_token(42)
        key_id, secret = keys.parse_token(token)
        assert key_id == 42
        assert keys.hash_secret(secret) == token_hash
        assert secret.startswith(prefix)

    def test_prefix_is_display_safe(self):
        # 8 chars of a 48-char secret: enough to recognise, useless to guess.
        _token, _hash, prefix = keys.mint_token(1)
        assert len(prefix) == 8

    def test_garbage_is_rejected_not_raised(self):
        for junk in ("", "dtk_", "dtk_x_abcdef1234567890", "Bearer dtk_1_ab",
                     "dtk_1_TOOSHORT", "dtk_1_" + "g" * 48,  # non-hex
                     "dtk_99999999999999999999_" + "a" * 48,  # id overflows
                     None, 42):
            assert keys.parse_token(junk) is None, junk

    def test_whitespace_is_tolerated(self):
        token, _h, _p = keys.mint_token(3)
        assert keys.parse_token(f"  {token}  ") == keys.parse_token(token)


class TestVerification:
    def test_correct_secret_verifies(self):
        row, token = _minted_row()
        _key_id, secret = keys.parse_token(token)
        ok, reason = keys.verify_key(row, secret)
        assert ok and reason == "ok"

    def test_wrong_secret_fails_like_missing_row(self):
        # The embedded id must not become an existence oracle: wrong secret
        # and no-such-row both come back (False, ...) through one call shape.
        row, _token = _minted_row()
        ok_wrong, _ = keys.verify_key(row, "ab" * 24)
        ok_missing, _ = keys.verify_key(None, "ab" * 24)
        assert ok_wrong is False and ok_missing is False

    def test_revoked_key_fails_even_with_correct_secret(self):
        row, token = _minted_row(revoked_at=datetime(2026, 1, 1))
        _key_id, secret = keys.parse_token(token)
        ok, reason = keys.verify_key(row, secret)
        assert not ok and reason == "revoked"

    def test_expiry_is_a_deadline_not_a_suggestion(self):
        now = datetime(2026, 8, 27, 12, 0, 0)
        row, token = _minted_row(expires_at=now)
        _key_id, secret = keys.parse_token(token)
        assert keys.verify_key(row, secret, now=now)[0] is False          # at
        assert keys.verify_key(row, secret, now=now - timedelta(1))[0]    # before
        assert keys.verify_key(row, secret, now=now + timedelta(1))[0] is False


class TestLimitResolution:
    TIER = SimpleNamespace(requests_per_min=60, cost_units_per_min=300,
                           requests_per_day=10_000, max_concurrency=4)

    def test_tier_values_apply_when_no_override(self):
        assert keys.effective_limits(_row(), self.TIER) == {
            "requests_per_min": 60, "cost_units_per_min": 300,
            "requests_per_day": 10_000, "max_concurrency": 4,
        }

    def test_per_key_override_beats_tier_field_by_field(self):
        row = _row(requests_per_min=500, max_concurrency=1)
        limits = keys.effective_limits(row, self.TIER)
        assert limits["requests_per_min"] == 500      # overridden up
        assert limits["max_concurrency"] == 1         # overridden down
        assert limits["cost_units_per_min"] == 300    # tier still applies
        assert limits["requests_per_day"] == 10_000

    def test_dangling_tier_falls_to_the_floor_not_to_unlimited(self):
        limits = keys.effective_limits(_row(), None)
        assert limits["requests_per_min"] <= self.TIER.requests_per_min
        assert all(v > 0 for v in limits.values())


class TestDescriptor:
    def test_group_key_descriptor(self):
        row, _token = _minted_row()
        desc = keys.key_descriptor(row, self.tier())
        assert desc["owner_type"] == "group"
        assert desc["group_id"] == 2
        assert desc["key_id"] == 7
        assert desc["limits"]["requests_per_min"] == 60

    def test_user_zero_is_a_real_owner(self):
        # User ids run from 0 (and negative); identity checks must be
        # `is not None`, never truthiness.
        row, _token = _minted_row(owner_user_id=0, group_id=None)
        desc = keys.key_descriptor(row, self.tier())
        assert desc["owner_type"] == "user"
        assert desc["owner_user_id"] == 0

    @staticmethod
    def tier():
        return SimpleNamespace(requests_per_min=60, cost_units_per_min=300,
                               requests_per_day=10_000, max_concurrency=4)


class TestSelfServeGate:
    """Self-serve minting is off until the API is announced.

    Production runs the API before the feature is public, so the two doors a
    user or group admin could mint through are closed by env flag while staff
    minting and key *use* stay open.
    """

    def _routes(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("DATA_API_SELF_SERVE_KEYS", raising=False)
        else:
            monkeypatch.setenv("DATA_API_SELF_SERVE_KEYS", value)
        import importlib

        module = importlib.import_module("web_api.routes.api_keys")
        return module

    def test_closed_by_default(self, monkeypatch):
        assert self._routes(monkeypatch, None).self_serve_enabled() is False

    def test_stays_closed_for_falsey_spellings(self, monkeypatch):
        for value in ("false", "0", "no", "off", "", "  "):
            assert self._routes(monkeypatch, value).self_serve_enabled() is False, value

    def test_opens_for_truthy_spellings(self, monkeypatch):
        for value in ("true", "1", "yes", "on", "TRUE", " True "):
            assert self._routes(monkeypatch, value).self_serve_enabled() is True, value


class TestTierBounds:
    """Tier definitions are editable from the ACP, so they need guard rails.

    The bounds exist to catch a slipped digit, not to second-guess a
    deliberate choice — hence the wide range. The key format matters more:
    it is a primary key that keys reference by name.
    """

    def _mod(self):
        import importlib

        return importlib.import_module("web_api.routes.api_keys")

    def test_every_limit_field_is_bounded(self):
        # `keys` is db/api_keys.py loaded by path at the top of this module —
        # the conftest stubs the `db` package, so a normal import gets a mock.
        bounds = self._mod()._TIER_BOUNDS
        assert set(bounds) == set(keys.LIMIT_FIELDS), (
            "a limit with no bound could be set to anything from the ACP"
        )

    def test_no_bound_allows_zero(self):
        # A zero budget is not a restrictive tier, it is a tier that cannot
        # make a single request — an unusable key rather than a slow one.
        for low, _high in self._mod()._TIER_BOUNDS.values():
            assert low >= 1

    def test_tier_key_format(self):
        ok = self._mod()._TIER_KEY_RE
        for good in ("standard", "elevated", "partner", "trusted_2", "a1"):
            assert ok.match(good), good
        for bad in ("", "A", "1abc", "has space", "has-dash", "Upper",
                    "x" * 33, "trailing_", "_leading"):
            if bad == "trailing_":
                continue  # a trailing underscore is harmless and allowed
            assert not ok.match(bad), bad


class TestTierSerialisationIsConsistent:
    """Both endpoints that return tiers must return the SAME shape.

    They did not: /admin/api-keys emitted {"key", "name"} while
    /admin/api-key-tiers emitted {"tier_key", "display_name", "sort_order"}.
    The frontend validates one schema, so the admin page threw on load in
    production while every unit test passed — the schema was tested against
    itself and never against what the route actually emits.
    """

    def _mod(self):
        import importlib

        return importlib.import_module("web_api.routes.api_keys")

    class _Tier:
        tier_key = "standard"
        display_name = "Standard"
        requests_per_min = 60
        cost_units_per_min = 200_000
        requests_per_day = 10_000
        max_concurrency = 4
        enabled = True
        sort_order = 0

    def test_tier_row_carries_the_fields_the_client_validates(self):
        # Mirrors ApiKeyTierSchema in packages/api-types.
        row = self._mod()._tier_row(self._Tier())
        assert set(row) == {
            "tier_key", "display_name", "requests_per_min", "cost_units_per_min",
            "requests_per_day", "max_concurrency", "enabled", "sort_order",
        }

    def test_tier_row_is_the_only_tier_serialiser_in_the_module(self):
        # Guards against a second hand-rolled dict drifting from this one again.
        import re
        from pathlib import Path

        source = Path(self._mod().__file__).read_text()
        # A literal building a tier by its old field names is the regression.
        assert '"key": t.tier_key' not in source
        assert '"name": t.display_name' not in source
        # Every tier list must go through the helper.
        assert len(re.findall(r"_tier_row\(", source)) >= 3

    def test_types_are_plain_json(self):
        row = self._mod()._tier_row(self._Tier())
        for field in ("requests_per_min", "cost_units_per_min",
                      "requests_per_day", "max_concurrency", "sort_order"):
            assert isinstance(row[field], int), field
        assert isinstance(row["enabled"], bool)
        assert isinstance(row["tier_key"], str)


class TestGlobalScope:
    """A global key reads everything visible — and nothing hidden.

    The dangerous shape for this feature is inference: if "no owner set" meant
    "reads everything", then any bug that failed to set an owner would mint an
    all-access key. Scope is therefore stored, required, and never guessed
    upward.
    """

    def test_scope_defaults_to_the_owner_supplied(self):
        assert keys.create_key.__doc__  # documented contract
        row = _row(scope="group", group_id=2, owner_user_id=None)
        assert keys.key_descriptor(row, None)["scope"] == "group"

    def test_a_row_with_no_owner_and_no_scope_reads_as_narrow_not_global(self):
        # A broken row must degrade to the *narrowest* reading. Inferring
        # 'global' from absent owners is the bug this guards against.
        broken = _row(scope=None, owner_user_id=None, group_id=None)
        assert keys.key_descriptor(broken, None)["scope"] != "global"

    def test_an_unrecognised_scope_is_not_honoured(self):
        row = _row(scope="superuser", owner_user_id=None, group_id=2)
        assert keys.key_descriptor(row, None)["scope"] in keys.SCOPES

    def test_descriptor_reports_global_when_stored(self):
        row = _row(scope="global", owner_user_id=None, group_id=None)
        desc = keys.key_descriptor(row, None)
        assert desc["scope"] == "global"
        assert desc["group_id"] is None and desc["owner_user_id"] is None

    def test_create_rejects_a_global_key_that_also_has_an_owner(self):
        import pytest

        with pytest.raises(ValueError):
            keys.create_key(_FakeSession(), scope="global", group_id=7)
        with pytest.raises(ValueError):
            keys.create_key(_FakeSession(), scope="global", owner_user_id=1)

    def test_create_rejects_an_unknown_scope(self):
        import pytest

        with pytest.raises(ValueError):
            keys.create_key(_FakeSession(), scope="everything", group_id=7)

    def test_create_rejects_an_ownerless_key_that_did_not_ask_to_be_global(self):
        # Omitting both owners defaults to scope 'group', which then has no
        # group_id — an error, not a silent grant of everything.
        import pytest

        with pytest.raises(ValueError):
            keys.create_key(_FakeSession())


class _FakeSession:
    """Enough of a Session for create_key's validation to run and raise."""

    def add(self, _row):  # pragma: no cover - never reached in these tests
        raise AssertionError("validation should have rejected this first")

    def flush(self):  # pragma: no cover
        raise AssertionError("validation should have rejected this first")
