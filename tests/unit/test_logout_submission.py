"""A "logout" submission books a hiscores read and stores nothing."""
import asyncio
from unittest.mock import MagicMock

import pytest

from data.submissions import dispatch


class TestRouting:
    def test_logout_is_a_supported_type_on_every_transport(self):
        assert dispatch.is_supported("logout")
        assert dispatch._PROCESSORS["logout"] == "logout_processor"
        # The webhook reader recognises it from the embed's own type field.
        assert dispatch.resolve_submission_type("logout", "Logout", ("type",), ("logout",)) == "logout"

    def test_it_is_about_the_main_game_only(self):
        assert "logout" not in dispatch.SEASONAL_TYPES
        assert "logout" not in dispatch.HARDCORE_TYPES


class TestProcessor:
    def _run(self, monkeypatch, data, player_id):
        import importlib
        import sys

        module = importlib.import_module("data.submissions.logout")
        session = MagicMock()
        session.query.return_value.filter.return_value.limit.return_value.scalar.return_value = player_id
        monkeypatch.setattr(module, "select_session_and_flag", lambda external: (session, True))
        booked = []
        poller = MagicMock()
        poller.note_logout = lambda conn, pid: booked.append(pid) or True
        monkeypatch.setitem(sys.modules, "services.hiscores_poller", poller)
        response = asyncio.run(module.logout_processor(data, external_session=session))
        return response, booked, session

    def test_a_known_account_gets_a_read_booked_and_nothing_written(self, monkeypatch):
        response, booked, session = self._run(monkeypatch, {"acc_hash": "123", "player_name": "x"}, 7)
        assert booked == [7] and response.success
        session.add.assert_not_called()
        session.commit.assert_not_called()

    def test_an_unknown_account_is_ignored_quietly(self, monkeypatch):
        response, booked, _ = self._run(monkeypatch, {"acc_hash": "123"}, None)
        assert booked == [] and response.success

    def test_no_account_hash_is_refused(self, monkeypatch):
        response, booked, _ = self._run(monkeypatch, {"player_name": "x"}, 7)
        assert booked == [] and not response.success
