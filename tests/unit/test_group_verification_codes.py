"""Group verification codes: the scheme and the throttled check.

What these pin:

* codes are nine digits shown as ``123-456-789``, and input is accepted with or
  without separators;
* only a keyed hash is stored, bound to the group, so the same digits never
  match another group and the stored value does not contain the code;
* issuing again replaces the code and bumps the generation; clearing withdraws
  it and bumps the generation too;
* a hash keeps verifying after a dedicated pepper is added later, because the
  hash records which key made it;
* with no key configured, issuing refuses instead of hashing weakly;
* the check is throttled per caller and per group, fails closed without Redis,
  and does not count malformed input as a guess.
"""
from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_real_gvc", _ROOT / "db" / "group_verification_codes.py")
gvc = importlib.util.module_from_spec(_spec)
sys.modules["_real_gvc"] = gvc
_spec.loader.exec_module(gvc)

Base = declarative_base()


class GroupVerificationCode(Base):
    __tablename__ = "group_verification_codes"
    group_id = sa.Column(sa.Integer, primary_key=True)
    code_hash = sa.Column(sa.String(80))
    generation = sa.Column(sa.Integer, nullable=False, default=0)
    issued_via = sa.Column(sa.String(16))
    issued_at = sa.Column(sa.DateTime)
    issued_by_user_id = sa.Column(sa.Integer)
    cleared_at = sa.Column(sa.DateTime)
    cleared_by_user_id = sa.Column(sa.Integer)
    cleared_reason = sa.Column(sa.String(32))


class FakeRedis:
    """Just enough of redis-py for the throttle: get, incr, expire(nx)."""

    def __init__(self):
        self.data = {}
        self.ttl = {}

    def get(self, key):
        return self.data.get(key)

    def pipeline(self):
        rc = self
        ops = []

        class _Pipe:
            def incr(self, key):
                ops.append(("incr", key))

            def expire(self, key, seconds, nx=False):
                ops.append(("expire", key, seconds, nx))

            def execute(self):
                for op in ops:
                    if op[0] == "incr":
                        rc.data[op[1]] = int(rc.data.get(op[1]) or 0) + 1
                    elif not (op[3] and op[1] in rc.ttl):
                        rc.ttl[op[1]] = op[2]

        return _Pipe()


@pytest.fixture()
def s(monkeypatch):
    monkeypatch.setattr(gvc, "GroupVerificationCode", GroupVerificationCode)
    monkeypatch.delenv("GROUP_CODE_PEPPER", raising=False)
    monkeypatch.setenv("JWT_TOKEN_KEY", "jwt-secret-for-tests")
    engine = sa.create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _digits(code: str) -> str:
    return code.replace("-", "")


class TestFormat:
    def test_generated_codes_are_nine_digits_in_three_groups(self):
        for _ in range(50):
            code = gvc.format_code(gvc.generate_digits())
            assert len(code) == 11 and code[3] == "-" and code[7] == "-"
            assert _digits(code).isdigit()

    def test_leading_zeros_survive(self, monkeypatch):
        monkeypatch.setattr(gvc.secrets, "randbelow", lambda n: 42)
        assert gvc.format_code(gvc.generate_digits()) == "000-000-042"

    @pytest.mark.parametrize("raw", ["123-456-789", "123 456 789", "123456789", " 123-456-789 "])
    def test_normalize_accepts_common_spellings(self, raw):
        assert gvc.normalize_code(raw) == "123456789"

    @pytest.mark.parametrize("raw", [None, "", "12345678", "1234567890", "123-456-78a", "１２３４５６７８９"])
    def test_normalize_rejects_anything_else(self, raw):
        assert gvc.normalize_code(raw) is None


class TestHashing:
    def test_stored_hash_is_keyed_versioned_and_hides_the_code(self, s):
        stored = gvc.hash_code(5, "123456789")
        assert stored.startswith("v1:j:")
        assert "123456789" not in stored
        assert gvc.matches(stored, 5, "123456789")
        assert not gvc.matches(stored, 5, "123456780")

    def test_hash_is_bound_to_the_group(self, s):
        stored = gvc.hash_code(5, "123456789")
        assert not gvc.matches(stored, 6, "123456789")

    def test_hash_depends_on_the_server_key(self, s, monkeypatch):
        stored = gvc.hash_code(5, "123456789")
        monkeypatch.setenv("JWT_TOKEN_KEY", "a-different-secret")
        assert not gvc.matches(stored, 5, "123456789")

    def test_adding_a_pepper_later_keeps_old_hashes_valid(self, s, monkeypatch):
        old = gvc.hash_code(5, "123456789")
        monkeypatch.setenv("GROUP_CODE_PEPPER", "dedicated-pepper")
        new = gvc.hash_code(5, "123456789")
        assert new.startswith("v1:p:")
        assert gvc.matches(old, 5, "123456789")
        assert gvc.matches(new, 5, "123456789")

    def test_no_key_refuses_to_issue(self, s, monkeypatch):
        for env in ("GROUP_CODE_PEPPER", "JWT_TOKEN_KEY", "ENCRYPTION_KEY"):
            monkeypatch.delenv(env, raising=False)
        with pytest.raises(gvc.CodeKeyMissing):
            gvc.hash_code(5, "123456789")

    @pytest.mark.parametrize("stored", [None, "", "garbage", "v0:j:abc", "v1:z:abc"])
    def test_malformed_stored_values_never_match(self, s, stored):
        assert not gvc.matches(stored, 5, "123456789")


class TestIssueAndClear:
    def test_first_issue_creates_the_row(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        row = gvc.get_row(s, 5)
        assert row.generation == 1 and row.issued_via == "reveal" and row.issued_by_user_id == 0
        assert gvc.matches(row.code_hash, 5, _digits(code))
        assert gvc.status(s, 5)["active"] is True

    def test_reissue_replaces_the_code_and_bumps_generation(self, s):
        first = gvc.issue_code(s, 5, 0, "reveal")
        second = gvc.issue_code(s, 5, 0, "reset")
        row = gvc.get_row(s, 5)
        assert row.generation == 2 and row.issued_via == "reset"
        assert gvc.matches(row.code_hash, 5, _digits(second))
        if first != second:
            assert not gvc.matches(row.code_hash, 5, _digits(first))

    def test_clear_withdraws_and_bumps_generation(self, s):
        gvc.issue_code(s, 5, 0, "reveal")
        assert gvc.clear_code(s, 5, 99, "staff") is True
        row = gvc.get_row(s, 5)
        assert row.code_hash is None and row.generation == 2
        assert row.cleared_by_user_id == 99 and row.cleared_reason == "staff"
        status = gvc.status(s, 5)
        assert status["active"] is False and status["cleared_reason"] == "staff"
        assert status["issued_at"] is None

    def test_clear_without_a_code_is_a_no_op(self, s):
        assert gvc.clear_code(s, 5, 99, "staff") is False
        gvc.issue_code(s, 5, 0, "reveal")
        gvc.clear_code(s, 5, 99, "staff")
        assert gvc.clear_code(s, 5, 99, "staff") is False
        assert gvc.get_row(s, 5).generation == 2

    def test_status_of_a_group_with_no_row(self, s):
        assert gvc.status(s, 5) == {
            "active": False, "issued_at": None, "issued_by_user_id": None,
            "issued_via": None, "cleared_at": None, "cleared_reason": None,
        }

    def test_status_never_carries_the_hash(self, s):
        gvc.issue_code(s, 5, 0, "reveal")
        assert "code_hash" not in gvc.status(s, 5)
        assert not any(str(v).startswith("v1:") for v in gvc.status(s, 5).values())


class TestCheck:
    def test_right_code(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        assert gvc.check_code(s, FakeRedis(), 5, code, "acc:1") == "ok"

    def test_wrong_code_is_counted(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        rc = FakeRedis()
        assert gvc.check_code(s, rc, 5, self._wrong(code), "acc:1") == "wrong"
        assert rc.data == {"gvc:fail:s:acc:1": 1, "gvc:fail:g:5": 1}
        assert rc.ttl == {"gvc:fail:s:acc:1": gvc.FAILURE_WINDOW_SECONDS,
                          "gvc:fail:g:5": gvc.FAILURE_WINDOW_SECONDS}

    def test_malformed_input_is_not_a_guess(self, s):
        gvc.issue_code(s, 5, 0, "reveal")
        rc = FakeRedis()
        assert gvc.check_code(s, rc, 5, "12-34", "acc:1") == "invalid"
        assert rc.data == {}

    def test_no_active_code(self, s):
        assert gvc.check_code(s, FakeRedis(), 5, "123-456-789", "acc:1") == "none"
        gvc.issue_code(s, 5, 0, "reveal")
        gvc.clear_code(s, 5, 99, "staff")
        assert gvc.check_code(s, FakeRedis(), 5, "123-456-789", "acc:1") == "none"

    def _wrong(self, code: str) -> str:
        digits = _digits(code)
        return gvc.format_code(f"{(int(digits) + 1) % 10 ** 9:09d}")

    def test_caller_is_locked_out_even_for_the_right_code(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        rc = FakeRedis()
        for _ in range(gvc.SUBJECT_FAILURES_MAX):
            assert gvc.check_code(s, rc, 5, self._wrong(code), "acc:1") == "wrong"
        assert gvc.check_code(s, rc, 5, code, "acc:1") == "throttled"
        # Another caller is unaffected until the group-wide cap.
        assert gvc.check_code(s, rc, 5, code, "acc:2") == "ok"

    def test_group_wide_cap_stops_distributed_guessing(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        rc = FakeRedis()
        for i in range(gvc.GROUP_FAILURES_MAX):
            assert gvc.check_code(s, rc, 5, self._wrong(code), f"acc:{i}") == "wrong"
        assert gvc.check_code(s, rc, 5, code, "acc:fresh") == "throttled"
        # The cap is per group.
        other = gvc.issue_code(s, 6, 0, "reveal")
        assert gvc.check_code(s, rc, 6, other, "acc:fresh") == "ok"

    def test_window_starts_at_first_failure(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        rc = FakeRedis()
        gvc.check_code(s, rc, 5, self._wrong(code), "acc:1")
        rc.ttl["gvc:fail:s:acc:1"] = 1  # pretend most of the window has passed
        gvc.check_code(s, rc, 5, self._wrong(code), "acc:1")
        assert rc.ttl["gvc:fail:s:acc:1"] == 1

    def test_no_redis_fails_closed(self, s):
        code = gvc.issue_code(s, 5, 0, "reveal")
        assert gvc.check_code(s, None, 5, code, "acc:1") == "throttled"
