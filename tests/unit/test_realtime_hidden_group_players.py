"""Per-group member hiding must reach the SSE live feed — ``web_api/routes/realtime.py``.

Two hiding layers exist, with different reach, and the stream previously knew
only the first:

* **global** — ``players.hidden`` / ``users.hidden``, via
  ``web_api.common.hidden_player_ids``; the player opted out of every public
  surface;
* **per-group** — an ``ignored_players`` row, written when a group's leaders
  hide a member from the admin member listing (PATCH
  /api/v1/groups/{id}/hidden-players). The lootboards have always honoured it
  and the Discord notifications do as of 2026-08-14, but ``rt:group:{id}``
  frames did not, so a hidden member still scrolled past in that group's live
  feed on the website.

The half of this that is easy to get wrong is the *scoping*: the per-group hide
must not leak outward onto the global/feed scopes, the player's own feed, or
another group's feed, so those directions are pinned as hard as the fix itself.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import web_api.routes.realtime as rt


def _frame(ftype="leaderboard_delta", pid=77, **data):
    """An rt:* envelope as services/realtime.py publishes it."""
    payload = {"id": pid, "name": "SomePlayer", "delta": 1_000_000}
    payload.update(data)
    return json.dumps({"v": 1, "type": ftype, "scope": "group:42", "data": payload})


def _filtered(monkeypatch, frame, scope, *, hidden=(), ignored=None):
    """Run the frame through the privacy filter; True == dropped.

    hidden:  globally hidden player ids.
    ignored: {group_id: {player_id, ...}} — the ignored_players rows.
    """
    ignored = ignored or {}
    monkeypatch.setattr(rt, "hidden_player_ids", lambda: set(hidden))
    monkeypatch.setattr(
        rt, "group_ignored_player_ids", lambda gid: set(ignored.get(gid, ()))
    )
    return asyncio.run(rt._is_hidden_event(frame, scope))


# --------------------------------------------------------------------------- #
# The fix: a group's hidden members disappear from that group's frames.
# --------------------------------------------------------------------------- #
class TestPerGroupHiding:
    def test_hidden_member_is_dropped_from_their_groups_scope(self, monkeypatch):
        assert _filtered(
            monkeypatch, _frame(pid=77), "group:42", ignored={42: {77}}
        ) is True

    def test_visible_member_still_passes(self, monkeypatch):
        assert _filtered(
            monkeypatch, _frame(pid=88), "group:42", ignored={42: {77}}
        ) is False

    @pytest.mark.parametrize(
        "ftype", ["drop", "leaderboard_delta", "personal_best", "pet",
                  "new_player", "subscription"]
    )
    def test_every_player_naming_frame_type_is_covered(self, monkeypatch, ftype):
        assert _filtered(
            monkeypatch, _frame(ftype, pid=77), "group:42", ignored={42: {77}}
        ) is True

    def test_player_id_key_is_honoured_too(self, monkeypatch):
        """Feed-ticker frames name the player in `player_id`, not `id`."""
        frame = json.dumps(
            {"v": 1, "type": "drop", "data": {"player_id": 77, "item_name": "Tbow"}}
        )
        assert _filtered(monkeypatch, frame, "group:42", ignored={42: {77}}) is True


# --------------------------------------------------------------------------- #
# The hide is group-scoped — it must not follow the player anywhere else.
# --------------------------------------------------------------------------- #
class TestHidingDoesNotLeakOutOfTheGroup:
    @pytest.mark.parametrize("scope", ["global", "feed", "player:77", "npc:12"])
    def test_other_scopes_are_untouched(self, monkeypatch, scope):
        assert _filtered(
            monkeypatch, _frame(pid=77), scope, ignored={42: {77}}
        ) is False

    def test_another_groups_feed_still_shows_them(self, monkeypatch):
        # Group 42 hid the player; group 99 did not and must still see them.
        assert _filtered(
            monkeypatch, _frame(pid=77), "group:99", ignored={42: {77}}
        ) is False

    def test_global_hiding_still_applies_everywhere(self, monkeypatch):
        # Regression: the pre-existing layer keeps its global reach.
        for scope in ("global", "feed", "player:77", "group:99"):
            assert _filtered(monkeypatch, _frame(pid=77), scope, hidden={77}) is True


# --------------------------------------------------------------------------- #
# Frames that name no player, and failure modes.
# --------------------------------------------------------------------------- #
class TestPassThrough:
    @pytest.mark.parametrize("ftype", ["announcement", "event_update", "chat_message"])
    def test_non_player_frames_pass_on_a_group_scope(self, monkeypatch, ftype):
        """`id` on these means an announcement/event/thread — filtering on it
        would drop unrelated frames whose id happens to collide."""
        assert _filtered(
            monkeypatch, _frame(ftype, pid=77), "group:42", ignored={42: {77}}
        ) is False

    def test_unparseable_frame_passes(self, monkeypatch):
        assert _filtered(monkeypatch, "not json", "group:42", ignored={42: {77}}) is False

    def test_non_integer_player_id_passes(self, monkeypatch):
        assert _filtered(
            monkeypatch, _frame(pid="77"), "group:42", ignored={42: {77}}
        ) is False

    def test_malformed_group_scope_is_not_filtered(self, monkeypatch):
        assert _filtered(
            monkeypatch, _frame(pid=77), "group:abc", ignored={42: {77}}
        ) is False

    def test_a_raising_lookup_fails_open(self, monkeypatch):
        """Fail open, matching hidden_player_ids and player_hidden_for_group: a
        transient DB fault must not blank out every group's live feed."""

        def _boom(gid):
            raise RuntimeError("database went away")

        monkeypatch.setattr(rt, "hidden_player_ids", lambda: set())
        monkeypatch.setattr(rt, "group_ignored_player_ids", _boom)
        assert asyncio.run(rt._is_hidden_event(_frame(pid=77), "group:42")) is False


# --------------------------------------------------------------------------- #
# Scope is read off the Redis channel, which is what actually routes the frame.
# --------------------------------------------------------------------------- #
class TestFrameScope:
    @pytest.mark.parametrize(
        "channel,expected",
        [
            (b"rt:group:42", "group:42"),
            ("rt:group:42", "group:42"),
            (b"rt:global", "global"),
            (b"rt:player:7", "player:7"),
            ("group:42", "group:42"),  # already stripped
            (None, ""),
        ],
    )
    def test_channel_to_scope(self, channel, expected):
        assert rt._frame_scope(channel) == expected

    def test_a_lying_envelope_scope_cannot_dodge_the_filter(self, monkeypatch):
        """The filter keys off the channel, not the envelope's `scope` field —
        the channel is what decides who receives the frame."""
        frame = json.dumps(
            {"v": 1, "type": "leaderboard_delta", "scope": "global",
             "data": {"id": 77}}
        )
        assert _filtered(monkeypatch, frame, rt._frame_scope(b"rt:group:42"),
                         ignored={42: {77}}) is True

    @pytest.mark.parametrize(
        "scope,expected", [("group:42", 42), ("group:abc", None), ("global", None),
                           ("player:42", None), ("", None), (None, None)]
    )
    def test_scope_group_id(self, scope, expected):
        assert rt._scope_group_id(scope) == expected


# --------------------------------------------------------------------------- #
# The cached lookup behind it (web_api/common.py).
# --------------------------------------------------------------------------- #
class TestGroupIgnoredPlayerIds:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        import web_api.common as common

        common._cache.clear()
        yield
        common._cache.clear()

    def _install(self, monkeypatch, rows, calls=None):
        import web_api.common as common

        class _Query:
            def filter(self, *a, **k):
                return self

            def all(self):
                return rows

        class _Session:
            def query(self, *a, **k):
                if calls is not None:
                    calls.append(1)
                return _Query()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(common, "db_session", lambda: _Session())
        return common

    def test_returns_the_groups_ignored_ids(self, monkeypatch):
        common = self._install(monkeypatch, [(77,), (88,)])
        assert common.group_ignored_player_ids(42) == {77, 88}

    def test_result_is_cached(self, monkeypatch):
        calls = []
        common = self._install(monkeypatch, [(77,)], calls)
        common.group_ignored_player_ids(42)
        common.group_ignored_player_ids(42)
        assert len(calls) == 1, "second call should be served from the cache"

    def test_cache_is_keyed_per_group(self, monkeypatch):
        calls = []
        common = self._install(monkeypatch, [(77,)], calls)
        common.group_ignored_player_ids(42)
        common.group_ignored_player_ids(99)
        assert len(calls) == 2, "a different group must not reuse group 42's set"

    @pytest.mark.parametrize("group_id", [0, None, "", "abc", -1])
    def test_absent_group_id_is_never_queried(self, monkeypatch, group_id):
        calls = []
        common = self._install(monkeypatch, [(77,)], calls)
        assert common.group_ignored_player_ids(group_id) == set()
        assert calls == []

    def test_db_error_fails_open(self, monkeypatch):
        import web_api.common as common

        def _boom():
            raise RuntimeError("database went away")

        monkeypatch.setattr(common, "db_session", _boom)
        assert common.group_ignored_player_ids(42) == set()
