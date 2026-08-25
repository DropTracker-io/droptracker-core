"""Points include/exclude lists: an item restricted to chosen drop sources.

The bug these cover: ``_point_list_entry_matches`` ANDs a row's item and NPC,
so a row saying "Contract of shard acquisition" + "Yama" matches nothing —
contracts are recorded under the reward container ("Dossier"), not the boss —
and a blacklisted item kept awarding points. The editor now offers the item's
real sources and stores one row per chosen source, with "all selected" meaning
the unrestricted NULL-npc row rather than an enumeration that a newly-seen
source would fall outside of.

Same scripted-session harness as the other route tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.points as pts
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import _S, _SessionCM


class FakeEntry:
    """Stands in for GroupPointBlacklist (a MagicMock under the stubbed ``db``
    package). Class attributes are the columns filters compare against."""

    group_id = MagicMock()
    list_type = MagicMock()
    id = MagicMock()

    def __init__(self, **kw):
        base = dict(id=1, group_id=2, list_type="blacklist", item_id=None, npc_id=None)
        base.update(kw)
        self.__dict__.update(base)


def _wire(monkeypatch, session, *, user_id=7):
    monkeypatch.setattr(pts, "current_user_id", lambda: user_id)
    monkeypatch.setattr(pts, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(pts, "_admin_ctx", lambda s, uid, gid, **kw: SimpleNamespace(user_id=uid))
    monkeypatch.setattr(pts, "GroupPointBlacklist", FakeEntry)
    # The response payload re-reads the table; the assertions here are about
    # what was written, so keep it out of the scripted batches.
    monkeypatch.setattr(pts, "_lists_payload", lambda s, gid: [])


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── _parse_list_target ───────────────────────────────────────────────────────

class TestParseListTarget:
    def test_item_only_is_unrestricted(self):
        # One item row, no sources — the caller stores npc_id NULL, which is
        # what the matcher treats as "from anywhere".
        item_id, npc_ids = pts._parse_list_target({"item_id": 30828}, _S([(30828,)]))
        assert (item_id, npc_ids) == (30828, [])

    def test_all_sources_deselected_to_a_subset(self):
        s = _S([(30828,)], [(232324,), (14176,)])
        item_id, npc_ids = pts._parse_list_target(
            {"item_id": 30828, "npc_ids": [232324, 14176]}, s
        )
        assert item_id == 30828
        assert npc_ids == [232324, 14176]

    def test_legacy_scalar_npc_id_still_accepted(self):
        item_id, npc_ids = pts._parse_list_target({"npc_id": 14176}, _S([(14176,)]))
        assert (item_id, npc_ids) == (None, [14176])

    def test_duplicate_ids_collapse(self):
        s = _S([(30828,)], [(232324,)])
        _item, npc_ids = pts._parse_list_target(
            {"item_id": 30828, "npc_ids": [232324, 232324, "232324"]}, s
        )
        assert npc_ids == [232324]

    def test_empty_and_zero_sources_read_as_any_source(self):
        # '0' sentinels reach here from legacy form state; they must not become
        # a row that matches npc 0 (i.e. nothing).
        _item, npc_ids = pts._parse_list_target(
            {"item_id": 30828, "npc_ids": [0, "0", None, ""]}, _S([(30828,)])
        )
        assert npc_ids == []

    def test_target_required(self):
        with pytest.raises(ProblemException) as exc:
            pts._parse_list_target({"npc_ids": []}, _S())
        assert exc.value.status == 400

    def test_unknown_npc_rejected(self):
        # Only 232324 comes back from npc_list — 999999 does not exist.
        s = _S([(30828,)], [(232324,)])
        with pytest.raises(ProblemException) as exc:
            pts._parse_list_target({"item_id": 30828, "npc_ids": [232324, 999999]}, s)
        assert exc.value.status == 400
        assert "999999" in exc.value.detail

    def test_unknown_item_rejected(self):
        with pytest.raises(ProblemException) as exc:
            pts._parse_list_target({"item_id": 424242}, _S([]))
        assert exc.value.status == 400

    def test_source_count_is_capped(self):
        ids = list(range(1, pts.MAX_LIST_ENTRY_SOURCES + 2))
        with pytest.raises(ProblemException) as exc:
            pts._parse_list_target({"item_id": 30828, "npc_ids": ids}, _S([(30828,)]))
        assert exc.value.status == 400
        assert "any source" in exc.value.detail

    def test_non_list_npc_ids_rejected(self):
        with pytest.raises(ProblemException) as exc:
            pts._parse_list_target({"item_id": 30828, "npc_ids": 232324}, _S())
        assert exc.value.status == 400


# ── POST /points/lists ───────────────────────────────────────────────────────

class TestCreateListEntry:
    @pytest.mark.asyncio
    async def test_sources_become_one_row_each(self, client, monkeypatch):
        # item lookup, npc lookup, existing-rows lookup
        s = _S([(30828,)], [(232324,), (14176,)], [])
        _wire(monkeypatch, s)
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "blacklist", "item_id": 30828, "npc_ids": [232324, 14176]},
        )
        assert resp.status_code == 200
        # The matcher ANDs item and npc per row, so "from either source" has to
        # be two rows — one row cannot hold two npc ids.
        assert [(r.item_id, r.npc_id) for r in s.added] == [(30828, 232324), (30828, 14176)]
        assert all(r.list_type == "blacklist" for r in s.added)

    @pytest.mark.asyncio
    async def test_no_sources_writes_one_unrestricted_row(self, client, monkeypatch):
        s = _S([(30828,)], [])
        _wire(monkeypatch, s)
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "blacklist", "item_id": 30828},
        )
        assert resp.status_code == 200
        # npc_id NULL, NOT an enumeration of today's known sources: a source we
        # have not observed yet must still be blacklisted.
        assert [(r.item_id, r.npc_id) for r in s.added] == [(30828, None)]

    @pytest.mark.asyncio
    async def test_existing_rows_are_not_duplicated(self, client, monkeypatch):
        existing = [FakeEntry(id=22, item_id=30828, npc_id=232324)]
        s = _S([(30828,)], [(232324,), (14176,)], existing)
        _wire(monkeypatch, s)
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "blacklist", "item_id": 30828, "npc_ids": [232324, 14176]},
        )
        assert resp.status_code == 200
        assert [(r.item_id, r.npc_id) for r in s.added] == [(30828, 14176)]

    @pytest.mark.asyncio
    async def test_fully_duplicate_entry_is_rejected(self, client, monkeypatch):
        existing = [FakeEntry(id=22, item_id=30828, npc_id=None)]
        # No npc_ids, so no npc lookup: item lookup, then existing rows.
        s = _S([(30828,)], existing)
        _wire(monkeypatch, s)
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "blacklist", "item_id": 30828},
        )
        assert resp.status_code == 409
        assert s.added == []

    @pytest.mark.asyncio
    async def test_dedupe_is_scoped_to_the_same_list(self, client, monkeypatch):
        """A whitelist row does not block the same target being blacklisted —
        the existing-rows query filters on list_type."""
        s = _S([(30828,)], [])
        _wire(monkeypatch, s)
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "whitelist", "item_id": 30828},
        )
        assert resp.status_code == 200
        assert s.added[0].list_type == "whitelist"

    @pytest.mark.asyncio
    async def test_bad_list_type_rejected(self, client, monkeypatch):
        _wire(monkeypatch, _S())
        resp = await client.post(
            "/api/v1/groups/2/points/lists",
            json={"list_type": "nope", "item_id": 30828},
        )
        assert resp.status_code == 400
