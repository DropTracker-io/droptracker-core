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
