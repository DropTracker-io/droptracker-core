"""
Discord Activity launch handler (Entry Point command, APP_HANDLER mode).

When Activities are enabled, Discord auto-creates a PRIMARY_ENTRY_POINT command
named "launch". Its default handler ``DISCORD_LAUNCH_ACTIVITY`` (2) makes Discord
post a public "started an activity" message to the channel EVERY time anyone
opens the app — noisy in busy servers. We instead run that command in
``APP_HANDLER`` (1) mode, so the launch click arrives here as an interaction and
we control the whole thing:

  1. open the Activity ourselves with the ``LAUNCH_ACTIVITY`` callback (type 12)
     — this launches the app with NO default channel message, and
  2. post our own message under our control.

By default that message is **ephemeral** (only the person who launched sees it),
so nothing clutters the channel — flip ``LAUNCH_MESSAGE_EPHEMERAL`` to False for
a public message, or gate it per-group inside ``build_launch_message``.

interactions.py 5.16 has no native entry-point / LAUNCH_ACTIVITY support, so we
hook the raw gateway interaction and respond over HTTP directly. The framework's
own dispatcher also sees this interaction, finds no registered command, and logs
a harmless ``Unknown cmd_id received`` — that is expected and can be ignored.

The matching command-handler flip (2 -> 1) is applied out-of-band via
``scripts/set_activity_entry_point_handler.py``; this extension is what makes
that flip safe (without it, APP_HANDLER launches would go unhandled).
"""
import logging

from interactions import Extension, listen
from interactions.api.events import RawGatewayEvent

log = logging.getLogger("interactions")

# Discord numeric constants (absent from interactions.py 5.16 enums).
_INTERACTION_APPLICATION_COMMAND = 2
_COMMAND_PRIMARY_ENTRY_POINT = 4
_CALLBACK_LAUNCH_ACTIVITY = 12
_MSG_FLAG_EPHEMERAL = 1 << 6  # 64

# Brand gold, matching the Activity's OSRS palette.
_ACCENT = 0xC8A24A

# When True the launch message is shown only to the launcher (no channel
# clutter — the point of this whole extension). Set False for a public message.
LAUNCH_MESSAGE_EPHEMERAL = True


def is_entry_point_interaction(data: dict) -> bool:
    """True iff this raw interaction is an Activity entry-point ("launch")
    command — the only thing this extension handles."""
    if not isinstance(data, dict) or data.get("type") != _INTERACTION_APPLICATION_COMMAND:
        return False
    return (data.get("data") or {}).get("type") == _COMMAND_PRIMARY_ENTRY_POINT


def build_launch_message(data: dict) -> dict | None:
    """The message posted when someone opens the app. Return None to send
    nothing. Ephemeral by default (see LAUNCH_MESSAGE_EPHEMERAL). `data` is the
    raw interaction, so this can branch on guild/channel later."""
    payload: dict = {
        "embeds": [
            {
                "title": "DropTracker is open",
                "description": (
                    "Your clan's live event boards, leaderboards, and your own "
                    "profile are all in here. Others can open it from the app "
                    "tray to join too."
                ),
                "color": _ACCENT,
            }
        ]
    }
    if LAUNCH_MESSAGE_EPHEMERAL:
        payload["flags"] = _MSG_FLAG_EPHEMERAL
    return payload


class ActivityLaunch(Extension):
    @listen("raw_interaction_create")
    async def _on_raw_interaction(self, event: RawGatewayEvent) -> None:
        data = event.data or {}
        if is_entry_point_interaction(data):
            await self._handle_launch(data)

    async def _handle_launch(self, data: dict) -> None:
        interaction_id = data.get("id")
        token = data.get("token")
        if not interaction_id or not token:
            return

        # 1) Open the Activity for the user — no default channel message.
        try:
            await self.bot.http.post_initial_response(
                {"type": _CALLBACK_LAUNCH_ACTIVITY}, interaction_id, token
            )
        except Exception:
            log.exception("[ActivityLaunch] LAUNCH_ACTIVITY callback failed")
            return  # launch failed; don't post a message about a non-launch

        # 2) Our own message (best-effort — the launch already succeeded).
        payload = build_launch_message(data)
        if not payload:
            return
        try:
            await self.bot.http.post_followup(payload, self.bot.app.id, token)
        except Exception:
            log.exception("[ActivityLaunch] launch follow-up message failed")


def setup(bot) -> None:
    ActivityLaunch(bot)
