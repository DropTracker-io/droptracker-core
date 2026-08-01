"""Regression tests for ensure_item_by_name's insert failure handling.

The name-based item path (clogs, manual submissions, adventure logs) minted
an ItemList row whenever the exact-name lookup missed. When the semantic API
resolved the name to an id that already exists — a spelling variant of a
known item, or a concurrent worker's insert winning the race — the
IntegrityError was swallowed by `except: return None` WITHOUT a rollback,
leaving the shared entry session pending-rollback and killing every later
query in that entry (observed 2026-08-01 00:59: a clog of item 29784
dead-lettered a whole multi-embed entry).
"""

from unittest.mock import AsyncMock, MagicMock

from data.submissions import common


class _FakeSemanticClient:
    def __init__(self, item_id):
        self._item_id = item_id
        self.semantic = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_item_id(self, item_name):
        return self._item_id


def _session(commit_exc=None, first_results=()):
    session = MagicMock()
    if commit_exc is not None:
        session.commit.side_effect = commit_exc
    session.query.return_value.filter.return_value.first.side_effect = list(first_results)
    return session


async def test_duplicate_insert_reuses_existing_row(monkeypatch):
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(29784))
    monkeypatch.setattr(common, "_ensure_item_icon", AsyncMock())

    existing = MagicMock()
    session = _session(
        commit_exc=RuntimeError("(1062, \"Duplicate entry '29784' for key 'PRIMARY'\")"),
        first_results=[None, existing],  # name lookup misses; by-id re-read hits
    )

    item = await common.ensure_item_by_name(session, "Araxyte venom sac")

    assert item is existing
    session.rollback.assert_called()  # session left usable, not pending-rollback


async def test_insert_failure_without_existing_row_returns_none_clean(monkeypatch):
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(29784))
    monkeypatch.setattr(common, "_ensure_item_icon", AsyncMock())

    session = _session(
        commit_exc=RuntimeError("server has gone away"),
        first_results=[None, None],
    )

    item = await common.ensure_item_by_name(session, "Araxyte venom sac")

    assert item is None
    session.rollback.assert_called()


async def test_successful_insert_unchanged(monkeypatch):
    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _FakeSemanticClient(29784))
    icon = AsyncMock()
    monkeypatch.setattr(common, "_ensure_item_icon", icon)

    session = _session(first_results=[None])

    item = await common.ensure_item_by_name(session, "Araxyte venom sac")

    assert item is not None
    session.commit.assert_called_once()
    icon.assert_awaited_once_with(29784)


async def test_semantic_api_failure_returns_none_without_insert(monkeypatch):
    class _Boom(_FakeSemanticClient):
        async def get_item_id(self, item_name):
            raise RuntimeError("api down")

    monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: _Boom(None))
    session = _session(first_results=[None])

    item = await common.ensure_item_by_name(session, "Araxyte venom sac")

    assert item is None
    session.add.assert_not_called()
