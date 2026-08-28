"""One-time key delivery.

A reveal exists so a freshly minted key can reach its owner without being
pasted into a Discord DM that keeps it forever. Three gates guard it — single
use, expiry, and audience — and the tests that matter are the ones proving
none of them can be skipped.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_real_reveals", _ROOT / "db" / "api_key_reveals.py")
reveals = importlib.util.module_from_spec(_spec)
sys.modules["_real_reveals"] = reveals
_spec.loader.exec_module(reveals)


def _row(**over):
    base = dict(
        id=1, reveal_token="tok", api_key_id=5, secret_ciphertext="cipher",
        audience_user_id=42, audience_group_id=None,
        expires_at=datetime.utcnow() + timedelta(hours=1),
        viewed_at=None, viewed_by_user_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestTokens:
    def test_tokens_are_long_and_unique(self):
        tokens = {reveals.new_reveal_token() for _ in range(200)}
        assert len(tokens) == 200
        assert all(len(t) >= 40 for t in tokens)


class TestAudience:
    def test_a_user_reveal_admits_only_that_user(self):
        row = _row(audience_user_id=42)
        assert reveals.may_view(None, row, 42)
        assert not reveals.may_view(None, row, 43)

    def test_signed_out_is_never_the_audience(self):
        # The link alone is not authorisation.
        assert not reveals.may_view(None, _row(), None)

    def test_user_zero_is_a_real_audience(self):
        # User ids run from 0; a truthiness check here would lock them out.
        assert reveals.may_view(None, _row(audience_user_id=0), 0)

    def test_a_reveal_with_no_audience_admits_nobody(self):
        row = _row(audience_user_id=None, audience_group_id=None)
        assert not reveals.may_view(None, row, 42)

    def test_group_reveal_fails_closed_when_the_role_lookup_errors(self):
        # An exception resolving a role is not permission.
        row = _row(audience_user_id=None, audience_group_id=7)
        assert not reveals.may_view(object(), row, 42)


class TestCreateValidation:
    def test_exactly_one_audience_is_required(self):
        class _S:
            def add(self, _r):
                raise AssertionError("should have been rejected")

        with pytest.raises(ValueError):
            reveals.create_reveal(_S(), api_key_id=1, plaintext="x")
        with pytest.raises(ValueError):
            reveals.create_reveal(_S(), api_key_id=1, plaintext="x",
                                  audience_user_id=1, audience_group_id=2)


class _Session:
    """Just enough session for claim(): one row, a commit flag."""

    def __init__(self, row):
        self._row = row
        self.committed = False

    def query(self, _model):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row

    def commit(self):
        self.committed = True


class TestClaim:
    def _claim(self, row, viewer=42, monkeypatch=None):
        # Bypass Fernet: the encryption is not what these tests are about.
        reveals.decrypt_secret = lambda c: "dtk_5_plaintext"
        return reveals.claim(_Session(row), "tok", viewer)

    def test_a_valid_claim_returns_the_secret(self):
        outcome, payload = self._claim(_row())
        assert outcome == reveals.OK
        assert payload["token"] == "dtk_5_plaintext"

    def test_claiming_burns_the_ciphertext_and_stamps_the_viewer(self):
        row = _row()
        self._claim(row)
        assert row.viewed_at is not None
        assert row.viewed_by_user_id == 42
        assert row.secret_ciphertext is None, "the secret must not survive its delivery"

    def test_a_second_claim_returns_nothing(self):
        row = _row()
        assert self._claim(row)[0] == reveals.OK
        assert self._claim(row)[0] == reveals.ALREADY_VIEWED

    def test_an_expired_link_is_refused(self):
        row = _row(expires_at=datetime.utcnow() - timedelta(seconds=1))
        assert self._claim(row)[0] == reveals.EXPIRED

    def test_the_wrong_viewer_is_refused_and_does_not_burn_it(self):
        row = _row(audience_user_id=42)
        outcome, _ = self._claim(row, viewer=43)
        assert outcome == reveals.FORBIDDEN
        # Crucially the real recipient can still use their link afterwards.
        assert row.viewed_at is None and row.secret_ciphertext == "cipher"

    def test_a_signed_out_visitor_is_refused(self):
        row = _row()
        assert self._claim(row, viewer=None)[0] == reveals.FORBIDDEN
        assert row.secret_ciphertext == "cipher"

    def test_a_missing_row_is_not_found(self):
        reveals.decrypt_secret = lambda c: "x"
        assert reveals.claim(_Session(None), "tok", 42)[0] == reveals.NOT_FOUND

    def test_a_row_marked_viewed_but_still_holding_ciphertext_is_spent(self):
        # Disagreeing flags must resolve to "no", not to handing it over.
        row = _row(viewed_at=datetime.utcnow())
        assert self._claim(row)[0] == reveals.ALREADY_VIEWED

    def test_a_row_with_no_ciphertext_is_spent(self):
        row = _row(secret_ciphertext=None)
        assert self._claim(row)[0] == reveals.ALREADY_VIEWED
