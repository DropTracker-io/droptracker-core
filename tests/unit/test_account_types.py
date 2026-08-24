"""Task 23 — OSRS account types (game modes) on players.

Covers the intake-side validation helper (data/submissions/common.py), the
varbit decoder shared with the state-sync path (utils/account_types.py), the
Web API v1 profile exposure (GET /players/{id}), and the OpenAPI contract.
"""

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


SPEC_ACCOUNT_TYPES = {
    "normal",
    "ironman",
    "ultimate_ironman",
    "hardcore_ironman",
    "group_ironman",
    "hardcore_group_ironman",
    "unranked_group_ironman",
}


# ── Intake helper: apply_account_type ─────────────────────────────────────────

class TestApplyAccountType:
    def _player(self, account_type=None):
        p = SimpleNamespace()
        p.account_type = account_type
        return p

    def test_enum_matches_spec(self):
        from data.submissions.common import VALID_ACCOUNT_TYPES

        assert set(VALID_ACCOUNT_TYPES) == SPEC_ACCOUNT_TYPES

    def test_valid_value_sets_field(self):
        from data.submissions.common import apply_account_type

        player = self._player()
        apply_account_type(player, "hardcore_ironman")
        assert player.account_type == "hardcore_ironman"

    def test_last_write_wins_on_deiron(self):
        from data.submissions.common import apply_account_type

        player = self._player(account_type="ironman")
        apply_account_type(player, "normal")
        assert player.account_type == "normal"

    def test_invalid_value_leaves_unchanged(self):
        from data.submissions.common import apply_account_type

        player = self._player(account_type="ironman")
        apply_account_type(player, "leagues_iron")
        assert player.account_type == "ironman"

    def test_absent_value_leaves_unchanged(self):
        from data.submissions.common import apply_account_type

        player = self._player(account_type="ironman")
        apply_account_type(player, None)
        assert player.account_type == "ironman"

    def test_normalizes_case_and_whitespace(self):
        from data.submissions.common import apply_account_type

        player = self._player()
        apply_account_type(player, "  Group_Ironman ")
        assert player.account_type == "group_ironman"

    def test_seasonal_world_is_ignored(self):
        from data.submissions.common import apply_account_type

        player = self._player(account_type="normal")
        apply_account_type(player, "ironman", world_type="seasonal")
        assert player.account_type == "normal"

    def test_never_raises(self):
        from data.submissions.common import apply_account_type

        # None player, non-string values: must be a silent no-op.
        apply_account_type(None, "ironman")
        apply_account_type(self._player(), 3)
        apply_account_type(self._player(), {"weird": "payload"})


# ── Varbit decoder (state-sync path) ──────────────────────────────────────────

class TestAccountTypeFromVarbit:
    # The table from the Task 23 spec: varbit 1777 value -> wire string.
    SPEC_TABLE = {
        0: "normal",
        1: "ironman",
        2: "ultimate_ironman",
        3: "hardcore_ironman",
        4: "group_ironman",
        5: "hardcore_group_ironman",
        6: "unranked_group_ironman",
    }

    def test_decodes_every_mode_in_the_spec_table(self):
        from utils.account_types import account_type_from_varbit

        for varbit, expected in self.SPEC_TABLE.items():
            assert account_type_from_varbit(varbit) == expected

    def test_enum_matches_the_intake_helper(self):
        from data.submissions.common import VALID_ACCOUNT_TYPES
        from utils.account_types import VALID_ACCOUNT_TYPES as SHARED

        # One source of truth: the two paths cannot drift apart.
        assert set(VALID_ACCOUNT_TYPES) == set(SHARED) == SPEC_ACCOUNT_TYPES

    def test_future_mode_degrades_to_none(self):
        from utils.account_types import account_type_from_varbit

        assert account_type_from_varbit(7) is None
        assert account_type_from_varbit(99) is None
        assert account_type_from_varbit(-1) is None

    def test_non_integer_values_are_none(self):
        from utils.account_types import account_type_from_varbit

        for value in (None, "1", "ironman", 1.5, {}, []):
            assert account_type_from_varbit(value) is None

    def test_bool_is_not_treated_as_an_index(self):
        from utils.account_types import account_type_from_varbit

        # bool subclasses int; True must not decode as "ironman".
        assert account_type_from_varbit(True) is None
        assert account_type_from_varbit(False) is None


# ── Profile lookup of the synced mode ─────────────────────────────────────────

class TestAccountTypeLookup:
    def _session_returning(self, row):
        s = MagicMock()
        s.query.return_value.filter.return_value.first.return_value = row
        return s

    def test_reads_the_synced_varbit(self):
        from web_api.routes.profiles import _account_type_for

        assert _account_type_for(self._session_returning((3,)), 42) == "hardcore_ironman"

    def test_never_synced_is_none(self):
        from web_api.routes.profiles import _account_type_for

        assert _account_type_for(self._session_returning(None), 42) is None

    def test_null_column_is_none(self):
        from web_api.routes.profiles import _account_type_for

        assert _account_type_for(self._session_returning((None,)), 42) is None

    def test_query_failure_is_none(self):
        from web_api.routes.profiles import _account_type_for

        s = MagicMock()
        s.query.side_effect = RuntimeError("table is gone")
        assert _account_type_for(s, 42) is None


# ── Web API v1: GET /players/{id} exposure ────────────────────────────────────

class TestPlayerProfileAccountType:
    @pytest.fixture()
    def client(self):
        import web_api

        return web_api.create_app().test_client()

    def _fake_session(self, player):
        s = MagicMock()
        s.query.return_value.filter.return_value.first.return_value = player
        (
            s.query.return_value.filter.return_value.filter.return_value
            .order_by.return_value.limit.return_value.all.return_value
        ) = []
        s.execute.return_value.fetchall.return_value = []
        return s

    def _patch_profiles(self, monkeypatch, player):
        import web_api.routes.profiles as profiles

        fake_session = self._fake_session(player)

        @contextmanager
        def fake_db_session():
            yield fake_session

        monkeypatch.setattr(profiles, "db_session", fake_db_session)
        # The stubbed models yield _ColExpr objects the real or_() rejects.
        monkeypatch.setattr(profiles, "or_", lambda *a, **k: True)
        monkeypatch.setattr(profiles, "NotifiedSubmission", MagicMock())
        monkeypatch.setattr(profiles, "player_month_total", lambda *a, **k: 0)
        monkeypatch.setattr(profiles, "player_global_rank", lambda *a, **k: None)
        monkeypatch.setattr(profiles, "_player_points", lambda *a, **k: None)
        monkeypatch.setattr(profiles, "_player_top_npc", lambda *a, **k: None)
        monkeypatch.setattr(profiles, "_build_submissions", lambda *a, **k: [])
        # Side-payload helpers irrelevant to this contract; neutralize the ones
        # present so the payload stays JSON-serializable with a mock session.
        for name, stub in [
            ("canonical_slug_for", lambda *a, **k: None),
            ("group_flairs", lambda *a, **k: {}),
            ("_rc", lambda *a, **k: None),
            ("cache_get", lambda *a, **k: []),
            ("_player_personal_bests", lambda *a, **k: []),
            ("_top_bosses_sql", lambda *a, **k: []),
        ]:
            monkeypatch.setattr(profiles, name, stub, raising=False)
        try:
            import web_api.routes.badges as badges

            monkeypatch.setattr(badges, "player_awards", lambda *a, **k: [], raising=False)
        except ImportError:
            pass

    def _fake_player(self, name, account_type):
        return SimpleNamespace(
            player_name=name,
            account_type=account_type,
            hidden=False,
            user=None,
            user_id=None,
        )

    async def test_account_type_returned_when_set(self, client, monkeypatch):
        player = self._fake_player("Iron Nik", "hardcore_ironman")
        self._patch_profiles(monkeypatch, player)

        r = await client.get("/api/v1/players/42")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["account_type"] == "hardcore_ironman"

    async def test_account_type_omitted_when_unset(self, client, monkeypatch):
        player = self._fake_player("Nik", None)
        self._patch_profiles(monkeypatch, player)

        r = await client.get("/api/v1/players/42")
        assert r.status_code == 200
        body = await r.get_json()
        assert "account_type" not in body

    async def test_synced_mode_is_used_when_the_row_is_empty(self, client, monkeypatch):
        import web_api.routes.profiles as profiles

        # The live case: nothing ever wrote the string, but the player syncs.
        player = self._fake_player("Iron NerdGuy", None)
        self._patch_profiles(monkeypatch, player)
        monkeypatch.setattr(profiles, "_account_type_for", lambda *a, **k: "ironman")

        r = await client.get("/api/v1/players/42")
        body = await r.get_json()
        assert body["account_type"] == "ironman"

    async def test_synced_mode_wins_over_a_stale_stored_string(self, client, monkeypatch):
        import web_api.routes.profiles as profiles

        # De-ironed: the state sync refreshes on login, the row may not.
        player = self._fake_player("Nik", "ironman")
        self._patch_profiles(monkeypatch, player)
        monkeypatch.setattr(profiles, "_account_type_for", lambda *a, **k: "normal")

        r = await client.get("/api/v1/players/42")
        body = await r.get_json()
        assert body["account_type"] == "normal"


# ── OpenAPI contract ──────────────────────────────────────────────────────────

class TestOpenApiContract:
    def _spec(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "web_api", "openapi.json"
        )
        with open(path) as f:
            return json.load(f)

    def test_player_profile_declares_account_type(self):
        spec = self._spec()
        profile = spec["components"]["schemas"]["PlayerProfile"]
        # PlayerProfile is an allOf of PlayerSummary + its own properties.
        own = next(part for part in profile["allOf"] if "properties" in part)
        account_type = own["properties"]["account_type"]
        assert account_type["type"] == "string"
        assert set(account_type["enum"]) == SPEC_ACCOUNT_TYPES
        # Optional on the contract: must not be required.
        assert "account_type" not in own.get("required", [])
