"""Regression tests for the ensure_npc_id_for_player insert race.

With the concurrent webhook consumer (and the webhook bot alongside it), two
workers can both miss the npc_list cache/DB lookups for a brand-new NPC, both
resolve it via the semantic API (a long await), and both INSERT the same
npc_id. The loser's IntegrityError used to be swallowed by a bare
`except: pass` WITHOUT a rollback, leaving the shared entry session in a
pending-rollback state that poisoned every later commit in the batch
(observed 2026-08-01: 'Blood-starved venator' npc_id=15770 dead-lettered a
whole entry). The fix mirrors ensure_item_for_drop: roll back and use the
row the winner created.
"""

from unittest.mock import AsyncMock, MagicMock

from data.submissions import common


class _FakeSemanticClient:
    def __init__(self, npc_id):
        self._npc_id = npc_id
        self.semantic = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_npc_id(self, npc_name):
        return self._npc_id


def _fresh_session(commit_exc=None, first_results=()):
    session = MagicMock()
    if commit_exc is not None:
        session.commit.side_effect = commit_exc
    # One shared .first() mock serves both the name lookup and the by-id
    # re-read; feed it results in call order.
    session.query.return_value.filter.return_value.first.side_effect = list(first_results)
    session.execute.return_value.first.return_value = None  # variant fallback misses
    return session


async def test_duplicate_insert_uses_winners_row(monkeypatch):
    monkeypatch.setattr(common, "npc_list", {})
    monkeypatch.setattr(common, "player_list", {"Tester": 123})
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(15770))

    winner_row = MagicMock()
    session = _fresh_session(
        commit_exc=RuntimeError("(1062, \"Duplicate entry '15770' for key 'PRIMARY'\")"),
        first_results=[None, winner_row],  # name lookup misses; by-id re-read hits
    )

    npc_id, name = await common.ensure_npc_id_for_player(
        session, "Blood-starved venator", 123, "Tester", True
    )

    assert npc_id == 15770
    assert name == "Blood-starved venator"
    session.rollback.assert_called()  # failed transaction cleared, session usable
    assert common.npc_list["Blood-starved venator"] == 15770


async def test_commit_failure_without_existing_row_falls_through_clean(monkeypatch):
    """A genuine insert failure still rolls back before the notification path."""
    monkeypatch.setattr(common, "npc_list", {})
    monkeypatch.setattr(common, "player_list", {"Tester": 123})
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(15770))
    notify = AsyncMock()
    monkeypatch.setattr(common, "create_notification", notify)

    session = _fresh_session(
        commit_exc=RuntimeError("server has gone away"),
        first_results=[None, None],  # re-read finds nothing either
    )

    npc_id, name = await common.ensure_npc_id_for_player(
        session, "Blood-starved venator", 123, "Tester", True
    )

    assert npc_id is None
    assert name == "Blood-starved venator"
    session.rollback.assert_called()  # no pending-rollback state leaks out
    notify.assert_awaited_once()


async def test_successful_insert_unchanged(monkeypatch):
    monkeypatch.setattr(common, "npc_list", {})
    monkeypatch.setattr(common, "player_list", {"Tester": 123})
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(15770))

    session = _fresh_session(first_results=[None])

    npc_id, name = await common.ensure_npc_id_for_player(
        session, "Blood-starved venator", 123, "Tester", True
    )

    assert npc_id == 15770
    assert name == "Blood-starved venator"
    session.commit.assert_called_once()
    assert common.npc_list["Blood-starved venator"] == 15770
