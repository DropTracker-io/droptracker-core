"""Unit tests for the Hall of Fame consolidation helpers (utils/hof.py).

The Hall of Fame extension runs in two processes while the legacy HOF Discord
application is retired: the core bot and the old droptracker-hof bot. These
tests pin the two invariants that keep that safe — exactly one owner per group,
and refresh signals that cannot ping-pong between the two queues.
"""

import json

from utils.hof import forwardable_refresh_payloads, hof_owner_is_self


class TestOwnershipPartition:
    def test_legacy_owns_unmigrated_groups(self):
        assert hof_owner_is_self(managed_by_core=False, is_legacy=True) is True

    def test_legacy_yields_migrated_groups(self):
        assert hof_owner_is_self(managed_by_core=True, is_legacy=True) is False

    def test_core_owns_migrated_groups(self):
        assert hof_owner_is_self(managed_by_core=True, is_legacy=False) is True

    def test_core_leaves_unmigrated_groups_alone(self):
        assert hof_owner_is_self(managed_by_core=False, is_legacy=False) is False

    def test_exactly_one_process_owns_any_group(self):
        """The invariant that prevents two writers on one channel."""
        for managed_by_core in (True, False):
            owners = [
                hof_owner_is_self(managed_by_core, is_legacy)
                for is_legacy in (True, False)
            ]
            assert owners.count(True) == 1, (
                f"managed_by_core={managed_by_core} produced {owners.count(True)} owners"
            )

    def test_falsy_flag_values_are_treated_as_unmigrated(self):
        # _managed_by_core answers False on a read error, which must mean
        # "legacy still owns it" rather than handing the group to nobody.
        assert hof_owner_is_self(None, is_legacy=True) is True
        assert hof_owner_is_self(None, is_legacy=False) is False


class TestRefreshForwarding:
    def test_untagged_payload_is_forwarded_once(self):
        items = [json.dumps({"player_id": 5, "npc_id": 9})]
        out = forwardable_refresh_payloads(items)
        assert len(out) == 1
        payload = json.loads(out[0])
        assert payload["player_id"] == 5
        assert payload["npc_id"] == 9
        assert payload["fwd"] == 1

    def test_already_forwarded_payload_is_dropped(self):
        items = [json.dumps({"player_id": 5, "npc_id": 9, "fwd": 1})]
        assert forwardable_refresh_payloads(items) == []

    def test_forwarding_converges_after_one_hop(self):
        """A signal neither process can place must not bounce forever."""
        items = [json.dumps({"player_id": 1, "npc_id": 2})]
        first_hop = forwardable_refresh_payloads(items)
        assert len(first_hop) == 1
        assert forwardable_refresh_payloads(first_hop) == []

    def test_accepts_bytes_from_redis(self):
        items = [json.dumps({"player_id": 7, "npc_id": 8}).encode("utf-8")]
        out = forwardable_refresh_payloads(items)
        assert json.loads(out[0])["player_id"] == 7

    def test_malformed_items_are_skipped_not_raised(self):
        items = ["not json", b"\xff\xfe", json.dumps([1, 2, 3]), "null"]
        assert forwardable_refresh_payloads(items) == []

    def test_mixed_batch_keeps_only_forwardable(self):
        items = [
            json.dumps({"player_id": 1, "npc_id": 1}),
            json.dumps({"player_id": 2, "npc_id": 2, "fwd": 1}),
            "garbage",
            json.dumps({"player_id": 3, "npc_id": 3}),
        ]
        out = forwardable_refresh_payloads(items)
        assert [json.loads(p)["player_id"] for p in out] == [1, 3]
