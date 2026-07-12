"""Unit tests for the per-group manual-submission policy (suggestion #45).

Loaded directly from the file path (same pattern as
test_event_scheduled_events.py) so the conftest ``db``/``utils`` stubs never
interfere — ``resolve_manual_moderation`` is pure by design.
"""

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "submissions", "manual_policy.py",
)
_spec = importlib.util.spec_from_file_location("_manual_policy_under_test", _MODULE_PATH)
mp = importlib.util.module_from_spec(_spec)
sys.modules["_manual_policy_under_test"] = mp
_spec.loader.exec_module(mp)


class TestResolveManualModeration:
    def test_allow_and_missing_policies_withhold_nothing(self):
        assert mp.resolve_manual_moderation({5: "allow", 6: None}, set()) == {}

    def test_unknown_policy_value_behaves_as_allow(self):
        # A corrupt config value must never silently withhold data.
        assert mp.resolve_manual_moderation({5: "banana"}, set()) == {}

    def test_block_always_excludes(self):
        # Even an authorized submitter is blocked, and permanently (excluded).
        assert mp.resolve_manual_moderation({5: "block"}, {5}) == {5: "excluded"}

    def test_authorized_only_excludes_unauthorized(self):
        policies = {5: "authorized_only", 6: "authorized_only"}
        assert mp.resolve_manual_moderation(policies, {6}) == {5: "excluded"}

    def test_authorized_only_allows_authorized(self):
        assert mp.resolve_manual_moderation({5: "authorized_only"}, {5}) == {}

    def test_confirm_holds_unauthorized_as_pending(self):
        assert mp.resolve_manual_moderation({5: "confirm"}, set()) == {5: "pending"}

    def test_confirm_bypassed_by_authorized(self):
        assert mp.resolve_manual_moderation({5: "confirm"}, {5}) == {}

    def test_system_groups_never_withheld(self):
        # 1 = template group, 2 = global: policies must not apply there —
        # global tracking is untouchable by group config.
        assert mp.resolve_manual_moderation({1: "block", 2: "block"}, set()) == {}

    def test_mixed_groups_are_independent(self):
        policies = {3: "allow", 4: "block", 5: "authorized_only", 6: "confirm", 7: "confirm"}
        # 4 blocked (excluded), 5 unauth (excluded), 6 unauth (pending),
        # 7 authorized (bypass).
        assert mp.resolve_manual_moderation(policies, {7}) == {
            4: "excluded", 5: "excluded", 6: "pending",
        }
