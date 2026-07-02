"""Unit tests for utils/partitions.py (Task 07 partition tokens)."""

from datetime import datetime

import utils.partitions as p


class TestTokens:
    def test_month_token(self):
        assert p.month_token(datetime(2026, 6, 1)) == "202606"
        assert p.month_token(datetime(2026, 12, 31)) == "202612"

    def test_week_token_format(self):
        # ISO week for 2026-06-17.
        tok = p.week_token(datetime(2026, 6, 17))
        assert p._WEEK_RE.match(tok)
        assert tok.startswith("2026W")

    def test_day_token(self):
        assert p.day_token(datetime(2026, 6, 17)) == "20260617"

    def test_all_tokens_contents(self):
        toks = p.all_tokens(datetime(2026, 6, 17))
        assert toks[0] == "202606"
        assert toks[2] == "20260617"
        assert toks[3] == "all"
        assert p._WEEK_RE.match(toks[1])


class TestValidation:
    def test_is_valid_token(self):
        assert p.is_valid_token("202606")
        assert p.is_valid_token("2026W27")
        assert p.is_valid_token("20260617")
        assert p.is_valid_token("all")
        assert not p.is_valid_token("2026")
        assert not p.is_valid_token("")
        assert not p.is_valid_token(None)

    def test_resolve_period_passthrough(self):
        assert p.resolve_period("202606") == "202606"
        assert p.resolve_period("20260617") == "20260617"
        assert p.resolve_period("2026w27") == "2026W27"  # normalized upper
        assert p.resolve_period("all") == "all"

    def test_resolve_period_fallback_to_month(self):
        assert p.resolve_period("bogus") == p.month_token()
        assert p.resolve_period(None) == p.month_token()
