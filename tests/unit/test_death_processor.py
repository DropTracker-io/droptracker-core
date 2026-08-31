"""What a death submission carries, and what survives into the queued payload.

The plugin has always sent far more than the four fields ``death_processor``
used to read. The rest — the killer's type, the PvP flag, the resolved area,
the safe/dangerous verdict, and (since 6.0.4) what the death cost — matter
twice over:

* the **row** is what the site and the DM render from;
* the **notification payload** is the ONLY thing the send-side gates can see.
  A key left out of it is a filter that silently cannot run, exactly the way
  dropping the envelope's ``_received_at`` made the month-boundary fix inert.

So these tests assert the threading, not just the parsing. Everything arrives
as a *string* (the plugin builds embed fields with ``String.valueOf``), and a
field it had no value for arrives as the literal ``"N/A"`` — the cases below
are the ones that actually turn up in production payloads.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db

CASTLE_WARS = 9520
VORKATH = 9023


class _FakePlayer:
    player_id = 5751994
    user_id = 2010
    user = MagicMock()
    player_name = "Ashey"


def _session():
    session = MagicMock()

    def _query(_model):
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    session.flush.return_value = None
    return session


#: Marks a key the plugin did not send at all — distinct from one it sent as "N/A".
_ABSENT = object()


def _payload(**overrides):
    """A 6.0.4 death payload, with every value in the string form the plugin sends."""
    payload = {
        "player_name": "Ashey",
        "acc_hash": "-5493473578450257792",
        "guid": "death-guid-1",
        "source": "Vorkath",
        "killer_type": "npc",
        "is_pvp": "false",
        "region_id": str(VORKATH),
        "region_name": "Ungael",
        "region_type": "BOSSES",
        "location": "Ungael",
        "is_safe_death": "false",
        "value_lost": "4200000",
        "value_kept": "18900000",
        "items_lost": "12",
        "p_v": "6.0.4",
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not _ABSENT}


def _run(payload, notify_deaths=True):
    """Run the processor and return (PlayerDeath kwargs, death notification data)."""
    import data.submissions.death as death

    player = _FakePlayer()
    session = _session()
    create_notification = AsyncMock()
    db.PlayerDeath.reset_mock()

    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(death, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("ensure_player_by_name_then_auth",
          new=AsyncMock(return_value=(player, True, True)))
        p("get_player_groups_with_global",
          new=MagicMock(return_value=[MagicMock(group_id=2, group_name="G")]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("attach_webhook_screenshot", new=AsyncMock(return_value=""))
        p("create_notification", new=create_notification)
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(patch(
            "utils.group_config.get",
            return_value="true" if notify_deaths else "false",
        ))
        asyncio.run(death.death_processor(payload, external_session=session))

    row_kwargs = db.PlayerDeath.call_args.kwargs if db.PlayerDeath.call_args else {}
    queued = {
        call.args[0]: call.args[2]
        for call in create_notification.call_args_list
        if call.args and call.args[0] == "death"
    }
    return row_kwargs, queued.get("death")


class TestNotificationPayload:
    """The queued payload is all the send-side gates ever see."""

    @pytest.mark.parametrize(
        "key",
        ["is_safe_death", "region_name", "region_type", "killer_type",
         "is_pvp", "value_lost", "value_kept", "items_lost"],
    )
    def test_every_filterable_field_reaches_the_queue(self, key):
        _, data = _run(_payload())
        assert data is not None, "no death notification was queued"
        assert key in data, (
            f"'{key}' never reaches notification_data, so the send-side gate "
            "cannot read it — the filter would silently not run"
        )

    def test_booleans_are_parsed_not_passed_through_as_strings(self):
        _, data = _run(_payload(is_safe_death="true", is_pvp="true"))
        assert data["is_safe_death"] is True
        assert data["is_pvp"] is True

    def test_values_are_parsed_as_integers(self):
        _, data = _run(_payload())
        assert data["value_lost"] == 4_200_000
        assert data["value_kept"] == 18_900_000
        assert data["items_lost"] == 12

    def test_a_payload_the_gates_can_act_on_is_actually_safe(self):
        from db.death_filter import is_safe_death

        _, data = _run(_payload(region_id=str(CASTLE_WARS), is_safe_death="true"))
        assert is_safe_death(data) is True


class TestParsing:
    def test_na_becomes_absent_not_a_literal_name(self):
        # addFields writes "N/A" for anything the plugin had no value for.
        row, data = _run(_payload(region_name="N/A", killer_type="N/A", region_type="N/A"))
        assert row["killer_type"] is None
        assert data["region_type"] is None

    def test_missing_value_is_unknown_not_zero(self):
        # A pre-6.0.4 client sends no value at all. Zero would tell the clan
        # the death was free; None says nobody knows.
        row, data = _run(_payload(value_lost=_ABSENT, value_kept=_ABSENT, items_lost=_ABSENT))
        assert row["value_lost"] is None
        assert data["value_lost"] is None

    def test_safe_flag_falls_back_to_the_region_for_old_clients(self):
        # No is_safe_death field: pre-6.0. The server classifies the region so
        # the group's setting still applies to that member.
        row, data = _run(_payload(is_safe_death=_ABSENT, region_id=str(CASTLE_WARS)))
        assert row["is_safe_death"] is True
        assert data["is_safe_death"] is True

    def test_dangerous_region_falls_back_the_other_way(self):
        row, _ = _run(_payload(is_safe_death=_ABSENT, region_id=str(VORKATH)))
        assert row["is_safe_death"] is False

    def test_region_name_falls_back_to_location(self):
        row, _ = _run(_payload(region_name=_ABSENT, location="Ungael"))
        assert row["region_name"] == "Ungael"


class TestPersistedRow:
    @pytest.mark.parametrize(
        "column, expected",
        [
            ("region_name", "Ungael"),
            ("killer_type", "npc"),
            ("is_pvp", False),
            ("is_safe_death", False),
            ("value_lost", 4_200_000),
        ],
    )
    def test_new_columns_are_written(self, column, expected):
        row, _ = _run(_payload())
        assert row[column] == expected
