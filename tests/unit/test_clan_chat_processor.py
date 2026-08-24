"""Unit tests for data/submissions/clan_chat.py — the game→Discord intake.

Only the pre-auth decisions are covered here (the house pattern: DB/services
are conftest-stubbed, so the session-bound tail is integration territory).
That is exactly where the bridge's loop guard sits.
"""
import asyncio

import pytest

from data.submissions import clan_chat as cc


@pytest.fixture
def stub_session(monkeypatch):
    """Processor stubbed down to its pre-auth decisions. Auth is made to FAIL
    so a line that reaches it is unmistakable — the echo drop must happen
    before, and it must not be mistaken for a rejection that came later."""
    monkeypatch.setattr(cc, "select_session_and_flag", lambda _ext: (object(), False))

    async def fake_auth(_session, _name, _hash, _key):
        return None, False, False

    monkeypatch.setattr(cc, "ensure_player_and_auth", fake_auth)


def _payload(sender, message="hello clan"):
    return {
        "message": message,
        "sender": sender,
        "clan_name": "The Best Clan",
        "player_name": "Relayer",
        "acc_hash": "12345",
    }


def _run(sender, **kwargs):
    return asyncio.run(cc.clan_chat_processor(_payload(sender, **kwargs)))


def test_own_discord_line_is_dropped_before_auth(stub_session):
    """The plugin renders Discord lines through client.addChatMessage, which
    posts a real ChatMessage — so a pre-fix build relays them back at us. Left
    unguarded, every Discord message is echoed into the channel it was typed
    in."""
    response = _run("Bob (Discord)")
    assert response.success is True
    assert "Discord" in response.message
    assert "auth" not in response.message.lower()


def test_echo_drop_survives_the_sender_cap(stub_session):
    """A Discord display name at the server's 32-char cap pushes the marker
    past SENDER_MAX_CHARS — the check runs on the uncapped sender."""
    long_sender = "n" * cc.SENDER_MAX_CHARS + " (Discord)"
    response = _run(long_sender)
    assert response.success is True
    assert "auth" not in response.message.lower()


def test_a_real_clanmate_line_is_not_treated_as_an_echo(stub_session):
    """Reaching the (failing) auth check is the proof it was not dropped as an
    echo."""
    response = _run("Iron Botanist")
    assert response.success is False
    assert "auth" in response.message.lower()


def test_missing_fields_still_rejected_before_the_echo_check(stub_session):
    response = _run("Bob (Discord)", message="   ")
    assert response.success is False
    assert "Missing" in response.message
