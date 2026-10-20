"""data/submissions/common.py — items known by name are minted from the game
cache first, the wiki last.

On 2026-10-20 the wiki had no page for 16 of The Fractured Archive's 17
collection log items hours after release, and 195 clog notifications were
dropped as "Item ... not found in database". The cache names an update's items
the moment it is published, and a clog submission carries the slot id besides.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.submissions import common
from utils import item_catalogue


@pytest.fixture
def cat(monkeypatch):
    names = {34646: "Orion", 34648: "Orion", 1069: "Steel platelegs",
             1070: "Steel platelegs"}
    real = {"orion": [34646, 34648], "steel platelegs": [1069]}  # 1070 is noted
    canon = {"orion": 34646, "steel platelegs": 1069}
    monkeypatch.setattr(item_catalogue, "item_name", lambda i: names.get(int(i)))
    monkeypatch.setattr(item_catalogue, "ids_for_name", lambda n: real.get(n.lower(), []))
    monkeypatch.setattr(item_catalogue, "canonical_item_id", lambda n: canon.get(n.lower()))


class TestGameItemId:
    def test_name_alone_uses_the_canonical_id(self, cat):
        assert common.game_item_id_for_name("Orion") == 34646

    def test_a_confirmed_client_id_wins(self, cat):
        assert common.game_item_id_for_name("Orion", "34648") == 34648

    def test_a_noted_client_id_is_ignored(self, cat):
        assert common.game_item_id_for_name("Steel platelegs", 1070) == 1069

    def test_a_client_id_with_another_name_is_ignored(self, cat):
        assert common.game_item_id_for_name("Orion", 1069) == 34646

    def test_newer_than_the_catalogue_trusts_the_client(self, cat):
        # Catalogue predates the update: neither the id nor the name is known.
        assert common.game_item_id_for_name("Living record (tier 9)", 40001) == 40001

    def test_unknown_id_but_known_name_prefers_the_cache(self, cat):
        assert common.game_item_id_for_name("Orion", 40001) == 34646

    @pytest.mark.parametrize("hint", [None, "", "null", "unknown", -1])
    def test_junk_hints(self, cat, hint):
        assert common.game_item_id_for_name("Orion", hint) == 34646
        assert common.game_item_id_for_name("Nothing", hint) is None


def _session(first_results):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = list(first_results)
    return session


class _Wiki:
    def __init__(self, item_id):
        self.item_id, self.calls, self.semantic = item_id, 0, self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_item_id(self, name):
        self.calls += 1
        return self.item_id


class TestEnsureItemByName:
    async def test_existing_row_wins_and_nothing_is_looked_up(self, cat, monkeypatch):
        existing = MagicMock()
        wiki = _Wiki(1)
        monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: wiki)
        item = await common.ensure_item_by_name(_session([existing]), "Orion", 34648)
        assert item is existing and wiki.calls == 0

    async def test_cache_mints_without_the_wiki(self, cat, monkeypatch):
        wiki = _Wiki(None)  # the wiki has no page yet
        monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: wiki)
        monkeypatch.setattr(common, "_ensure_item_icon", AsyncMock())
        session = _session([None])
        item = await common.ensure_item_by_name(session, "Orion")
        assert item.item_id == 34646 and wiki.calls == 0
        session.commit.assert_called_once()

    async def test_wiki_is_the_fallback_for_names_the_cache_lacks(self, cat, monkeypatch):
        wiki = _Wiki(555)
        monkeypatch.setattr(common.osrs_api, "create_client", lambda *a, **k: wiki)
        monkeypatch.setattr(common, "_ensure_item_icon", AsyncMock())
        item = await common.ensure_item_by_name(_session([None]), "Wiki-only spelling")
        assert item.item_id == 555 and wiki.calls == 1

    async def test_item_id_zero_is_a_real_item(self, monkeypatch):
        monkeypatch.setattr(common, "game_item_id_for_name", lambda *a, **k: 0)
        monkeypatch.setattr(common, "_ensure_item_icon", AsyncMock())
        item = await common.ensure_item_by_name(_session([None]), "Dwarf remains")
        assert item is not None and item.item_id == 0
