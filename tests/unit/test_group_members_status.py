"""Members page: who has linked Discord and who runs the plugin (ticket 521).

A clan of 500 asked which members had installed the plugin and linked their
Discord, rather than a flat list. The members endpoint now reports both per
member, a roster-wide summary, and a ``status`` filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import web_api.routes.group_admin as ga

NOW = datetime(2026, 11, 10, 12, 0)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _Session:
    def __init__(self, *results):
        self._results = list(results)

    def execute(self, *_a, **_k):
        return _Result(self._results.pop(0))


class TestPluginStatus:
    def test_seen_this_month_is_active(self):
        assert ga.member_plugin_status(NOW - timedelta(days=3), False, NOW) == "active"

    def test_seen_long_ago_is_inactive(self):
        assert ga.member_plugin_status(NOW - timedelta(days=45), True, NOW) == "inactive"

    def test_drops_from_before_sightings_existed_count_as_used(self):
        # player_plugin_versions starts 2026-10-04; older users only have drops.
        assert ga.member_plugin_status(None, True, NOW) == "inactive"

    def test_nothing_at_all_is_never(self):
        assert ga.member_plugin_status(None, False, NOW) == "never"


class TestStatusFilter:
    MEMBERS = [
        {"id": 1, "discord_linked": True, "plugin": "active"},
        {"id": 2, "discord_linked": False, "plugin": "inactive"},
        {"id": 0, "discord_linked": True, "plugin": "never"},
    ]

    def _ids(self, status):
        return [m["id"] for m in self.MEMBERS if ga.member_matches_status(m, status)]

    def test_each_filter(self):
        assert self._ids("linked") == [1, 0]
        assert self._ids("not_linked") == [2]
        assert self._ids("plugin") == [1, 2]
        assert self._ids("plugin_active") == [1]
        assert self._ids("no_plugin") == [0]

    def test_unknown_status_keeps_everyone(self):
        assert self._ids("") == [1, 2, 0]

    def test_every_advertised_filter_is_handled(self):
        for status in ga.MEMBER_STATUS_FILTERS:
            assert len(self._ids(status)) < len(self.MEMBERS)


class TestPluginState:
    def test_latest_sighting_wins(self):
        older, newer = NOW - timedelta(days=9), NOW - timedelta(days=1)
        s = _Session(
            [(7, "6.1.18", older), (7, "6.1.19", newer), (0, "6.1.19", older)],
            [(7,), (9,)],
        )
        seen, tracked = ga._member_plugin_state(s, [7, 0, 9])
        assert seen == {7: (newer, "6.1.19"), 0: (older, "6.1.19")}
        assert tracked == {7, 9}

    def test_empty_roster_runs_no_query(self):
        assert ga._member_plugin_state(_Session(), []) == ({}, set())
