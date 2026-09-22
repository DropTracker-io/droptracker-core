"""Targeted pop-up notices (web118a): audience rules, matching, validation.

The audience module's pure half decides who sees a notice; these tests pin
those decisions without a database. The route helpers covered here are the
ones that guard what staff can store (links, schedule) and what a visitor is
shown (window, lifecycle).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from web_api.popup_audience import (
    FREE_TIER,
    AudienceError,
    ViewerFacts,
    audience_matches,
    describe_audience,
    facts_needed,
    normalize_audience,
    parse_stored_audience,
    rule_matches,
)


# ── normalize_audience ────────────────────────────────────────────────────────

class TestNormalizeAudience:
    def test_everyone_swallows_other_rules(self):
        rules = normalize_audience([{"type": "users", "user_ids": [5]}, {"type": "everyone"}])
        assert rules == [{"type": "everyone"}]

    def test_accepts_json_text(self):
        assert normalize_audience('[{"type": "staff"}]') == [{"type": "staff"}]

    def test_users_dedupes_and_keeps_user_zero_and_negative_ids(self):
        # user_id 0 is the real primary superadmin; ids also run negative.
        rules = normalize_audience([{"type": "users", "user_ids": [0, "0", -3, 7, 7]}])
        assert rules == [{"type": "users", "user_ids": [0, -3, 7]}]

    def test_bool_is_not_an_id(self):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "users", "user_ids": [True]}])

    def test_users_needs_someone(self):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "users", "user_ids": []}])

    @pytest.mark.parametrize("raw", [None, [], "not json", {"type": "everyone"}, "{}"])
    def test_rejects_empty_or_malformed(self, raw):
        with pytest.raises(AudienceError):
            normalize_audience(raw)

    def test_unknown_type(self):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "martians"}])

    def test_leader_roles_default_to_owner_and_keep_canonical_order(self):
        assert normalize_audience([{"type": "group_leaders"}])[0]["roles"] == ["owner"]
        rule = normalize_audience([{"type": "group_leaders", "roles": ["admin", "owner"]}])[0]
        assert rule["roles"] == ["owner", "admin"]

    @pytest.mark.parametrize("roles", [[], ["member"], ["owner", "member"], "owner"])
    def test_leader_roles_must_be_owner_or_admin(self, roles):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "group_leaders", "roles": roles}])

    def test_group_rules_get_empty_filters(self):
        rule = normalize_audience([{"type": "group_members"}])[0]
        assert rule == {"type": "group_members", "group_ids": [], "group_tiers": []}

    def test_tier_keys_are_checked_for_shape(self):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "supporters", "tier_keys": ["t3; drop table"]}])

    def test_rule_count_is_capped(self):
        with pytest.raises(AudienceError):
            normalize_audience([{"type": "staff"}] * 11)


class TestParseStoredAudience:
    def test_unreadable_audience_matches_nobody(self):
        # Fail closed: a broken row must never widen to everyone.
        assert parse_stored_audience("garbage") == []
        assert parse_stored_audience(None) == []
        assert not audience_matches(parse_stored_audience("[]"), ViewerFacts(user_id=1))


# ── matching ──────────────────────────────────────────────────────────────────

def _facts(**kw):
    return ViewerFacts(user_id=kw.pop("user_id", 42), **kw)


class TestRuleMatches:
    def test_everyone(self):
        assert rule_matches({"type": "everyone"}, _facts())

    def test_staff(self):
        assert rule_matches({"type": "staff"}, _facts(is_staff=True))
        assert not rule_matches({"type": "staff"}, _facts())

    def test_users_including_user_zero(self):
        rule = {"type": "users", "user_ids": [0, 9]}
        assert rule_matches(rule, _facts(user_id=0))
        assert not rule_matches(rule, _facts(user_id=42))

    def test_leaders_by_role(self):
        owners = {"type": "group_leaders", "roles": ["owner"], "group_ids": [], "group_tiers": []}
        both = dict(owners, roles=["owner", "admin"])
        admin_somewhere = _facts(leader_roles={10: "admin"})
        assert not rule_matches(owners, admin_somewhere)
        assert rule_matches(both, admin_somewhere)
        assert rule_matches(owners, _facts(leader_roles={10: "owner"}))

    def test_leaders_limited_to_groups(self):
        rule = {"type": "group_leaders", "roles": ["owner"], "group_ids": [11], "group_tiers": []}
        assert not rule_matches(rule, _facts(leader_roles={10: "owner"}))
        assert rule_matches(rule, _facts(leader_roles={10: "owner", 11: "owner"}))

    def test_role_and_group_must_hold_on_the_same_group(self):
        # Owner of 10 and admin of 11 is not "owner of 11".
        rule = {"type": "group_leaders", "roles": ["owner"], "group_ids": [11], "group_tiers": []}
        assert not rule_matches(rule, _facts(leader_roles={10: "owner", 11: "admin"}))

    def test_leaders_by_group_tier(self):
        rule = {"type": "group_leaders", "roles": ["owner"], "group_ids": [], "group_tiers": ["t3"]}
        assert rule_matches(rule, _facts(leader_roles={10: "owner"}, group_tiers={10: "t3"}))
        assert not rule_matches(rule, _facts(leader_roles={10: "owner"}, group_tiers={10: "t2"}))

    def test_group_without_paid_tier_is_free(self):
        rule = {"type": "group_leaders", "roles": ["owner"], "group_ids": [], "group_tiers": [FREE_TIER]}
        assert rule_matches(rule, _facts(leader_roles={10: "owner"}))
        assert not rule_matches(rule, _facts(leader_roles={10: "owner"}, group_tiers={10: "t2"}))

    def test_members(self):
        rule = {"type": "group_members", "group_ids": [3], "group_tiers": []}
        assert rule_matches(rule, _facts(member_group_ids={1, 3}))
        assert not rule_matches(rule, _facts(member_group_ids={1}))
        any_group = {"type": "group_members", "group_ids": [], "group_tiers": []}
        assert rule_matches(any_group, _facts(member_group_ids={1}))
        assert not rule_matches(any_group, _facts())

    def test_supporters_any_tier(self):
        rule = {"type": "supporters", "tier_keys": []}
        assert rule_matches(rule, _facts(is_supporter=True))
        assert not rule_matches(rule, _facts())

    def test_supporters_by_tier(self):
        rule = {"type": "supporters", "tier_keys": ["supporter", "t3"]}
        assert rule_matches(rule, _facts(is_supporter=True, supporter_tiers={"t3"}))
        assert not rule_matches(rule, _facts(is_supporter=True, supporter_tiers={"basic"}))

    def test_audience_is_a_union(self):
        rules = [
            {"type": "users", "user_ids": [1]},
            {"type": "staff"},
        ]
        assert audience_matches(rules, _facts(user_id=1))
        assert audience_matches(rules, _facts(user_id=2, is_staff=True))
        assert not audience_matches(rules, _facts(user_id=2))


class TestFactsNeeded:
    def test_only_what_rules_use(self):
        assert facts_needed([[{"type": "everyone"}], [{"type": "users", "user_ids": [1]}]]) == set()
        leaders = {"type": "group_leaders", "roles": ["owner"], "group_ids": [], "group_tiers": []}
        assert facts_needed([[leaders]]) == {"leaders"}
        tiered = dict(leaders, group_tiers=["t3"])
        assert facts_needed([[tiered], [{"type": "staff"}]]) == {"leaders", "group_tiers", "staff"}
        assert facts_needed([[{"type": "supporters", "tier_keys": []}]]) == {"supporters"}


class TestDescribeAudience:
    def test_summaries(self):
        rules = [
            {"type": "group_leaders", "roles": ["owner"], "group_ids": [], "group_tiers": ["t3"]},
            {"type": "users", "user_ids": [1, 2]},
        ]
        text = describe_audience(rules, {("tier", "t3"): "Tier 3"})
        assert text == "Clan owners of Tier 3 groups + 2 people"
        assert describe_audience([{"type": "everyone"}]) == "Everyone signed in"
        assert describe_audience([{"type": "supporters", "tier_keys": []}]) == "All supporters"


# ── route helpers ─────────────────────────────────────────────────────────────

class TestRouteHelpers:
    def _problem(self):
        from web_api.common import ProblemException

        return ProblemException

    def test_state_of(self):
        from web_api.routes.popup_notices import state_of

        now = datetime(2026, 9, 22, 12, 0)
        n = SimpleNamespace(status="live", starts_at=None, expires_at=None)
        assert state_of(n, now) == "live"
        n.starts_at = now + timedelta(hours=1)
        assert state_of(n, now) == "scheduled"
        n.starts_at, n.expires_at = None, now
        assert state_of(n, now) == "expired"
        n.status = "ended"
        assert state_of(n, now) == "ended"
        n.status = "draft"
        assert state_of(n, now) == "draft"

    def test_in_window_needs_rules(self):
        from web_api.routes.popup_notices import _in_window

        now = datetime(2026, 9, 22, 12, 0)
        n = {"starts_at": None, "expires_at": None, "rules": [{"type": "everyone"}]}
        assert _in_window(n, now)
        assert not _in_window(dict(n, rules=[]), now)
        assert not _in_window(dict(n, starts_at=now + timedelta(seconds=1)), now)
        assert not _in_window(dict(n, expires_at=now), now)

    def _fields(self, **kw):
        from web_api.routes.popup_notices import _validate_fields

        body = {"title": "Hello", "body_md": "**Hi**"}
        body.update(kw)
        return _validate_fields(body, partial=False)

    def test_defaults(self):
        out = self._fields()
        assert out["tone"] == "info" and out["size"] == "md"
        assert out["cta_label"] is None and out["cta_url"] is None
        assert out["starts_at"] is None and out["expires_at"] is None

    @pytest.mark.parametrize("url", ["/premium", "https://runelite.net", "HTTP://example.com/x"])
    def test_button_links_allowed(self, url):
        assert self._fields(cta_label="Go", cta_url=url)["cta_url"] == url

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "//evil.example", "premium", "data:text/html,x"])
    def test_button_links_rejected(self, url):
        with pytest.raises(self._problem()):
            self._fields(cta_label="Go", cta_url=url)

    def test_button_needs_label_and_link(self):
        with pytest.raises(self._problem()):
            self._fields(cta_label="Go")
        with pytest.raises(self._problem()):
            self._fields(cta_url="/premium")

    @pytest.mark.parametrize("kw", [{"title": ""}, {"title": "x" * 121}, {"body_md": "   "},
                                    {"tone": "loud"}, {"size": "xl"}, {"expires_at": "soon"},
                                    {"starts_at": True}])
    def test_rejects_bad_fields(self, kw):
        with pytest.raises(self._problem()):
            self._fields(**kw)

    def test_partial_only_reads_supplied_keys(self):
        from web_api.routes.popup_notices import _validate_fields

        assert _validate_fields({"tone": "important"}, partial=True) == {"tone": "important"}

    def test_window_checks(self):
        from web_api.routes.popup_notices import _check_window

        now = datetime.now()
        with pytest.raises(self._problem()):
            _check_window(now, now - timedelta(minutes=1), sending=False)
        with pytest.raises(self._problem()):
            _check_window(None, now - timedelta(minutes=1), sending=True)
        # Editing a live notice may move its end into the past (ends it).
        _check_window(None, now - timedelta(minutes=1), sending=False)
        _check_window(None, now + timedelta(days=1), sending=True)
