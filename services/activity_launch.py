"""
Discord Activity launch handler (Entry Point command, APP_HANDLER mode) + the
standing "Open DropTracker" card.

Entry point: Discord's default handler (DISCORD_LAUNCH_ACTIVITY) posts a public
"started an activity" message every time anyone opens the app. We run the
"launch" command in APP_HANDLER mode instead, so the click arrives here and we
answer with the LAUNCH_ACTIVITY callback (type 12) — the app opens with NO
default channel message. (scripts/set_activity_entry_point_handler.py flips the
handler; this extension is what makes that flip safe.)

Standing card: a group leader picks a channel (``activity_launch_channel`` group
config) and the bot keeps one Components-V2 card there. Its button is a normal
custom_id button; clicking it is the same kind of interaction as the entry
point, so it's answered the same way (a LAUNCH_ACTIVITY response). A ~60s
reconcile sweep posts the card, moves it when the channel changes, and removes
it when the channel is cleared.

interactions.py 5.16 has no native entry-point / LAUNCH_ACTIVITY support, so we
hook the raw gateway interaction and respond over HTTP. Its own dispatcher also
sees the interaction, finds no registered command/component, and logs a harmless
"Unknown cmd_id received" — expected.

The pure decision logic lives in ``services/activity_launch_core.py`` (no
interactions import, so it's unit-testable); this module is the Discord shell.
"""
import functools
import logging

from interactions import Extension, listen
from interactions.api.events import RawGatewayEvent

from services import activity_launch_core as core
from services.activity_launch_core import (  # re-exported for callers/tests
    CALLBACK_LAUNCH_ACTIVITY,
    LAUNCH_BUTTON_CUSTOM_ID,
    build_launch_message,
    is_entry_point_interaction,
    is_launch_button_interaction,
)

log = logging.getLogger("interactions")


def build_launch_card():
    """Components-V2 "Open DropTracker" card. Its button (custom_id
    LAUNCH_BUTTON_CUSTOM_ID) opens the Activity when clicked. Matches the bot's
    other V2 cards (services/components.py)."""
    from interactions import ActionRow, Button, ButtonStyle, UnfurledMediaItem
    from interactions.models import (
        ContainerComponent,
        SectionComponent,
        SeparatorComponent,
        TextDisplayComponent,
        ThumbnailComponent,
    )

    logo = UnfurledMediaItem(url="https://www.droptracker.io/img/droptracker-small.gif")
    return [
        ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(content="## Open DropTracker"),
                    TextDisplayComponent(
                        content=(
                            "-# Your clan's live event boards, leaderboards, personal "
                            "bests and your own profile — right here in Discord."
                        )
                    ),
                ],
                accessory=ThumbnailComponent(media=logo),
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "Tap **Open DropTracker** to launch the app. Follow live bingo "
                    "boards, browse the leaderboards, and sign up for events without "
                    "leaving Discord."
                )
            ),
            SeparatorComponent(divider=True),
            ActionRow(
                Button(
                    label="Open DropTracker",
                    style=ButtonStyle.BLURPLE,
                    custom_id=LAUNCH_BUTTON_CUSTOM_ID,
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content="-# Powered by the [DropTracker](https://www.droptracker.io)"),
        )
    ]


async def _post_card(bot, channel_id):
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return None
        return await channel.send(components=build_launch_card())
    except Exception:
        log.warning("[ActivityLaunch] couldn't post card to channel %s", channel_id, exc_info=True)
        return None


async def _delete_message(bot, channel_id, message_id) -> None:
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return
        old = await channel.fetch_message(message_id=message_id)
        await old.delete()
    except Exception:
        # Already gone, no perms, channel deleted — nothing to recover.
        log.debug("[ActivityLaunch] couldn't delete stale card %s/%s", channel_id, message_id, exc_info=True)


async def reconcile_all(bot, session_factory) -> None:
    """Wire the bot's Discord side effects into the pure reconcile sweep."""
    await core.reconcile_all(
        session_factory,
        post_card=functools.partial(_post_card, bot),
        delete_message=functools.partial(_delete_message, bot),
    )


class ActivityLaunch(Extension):
    @listen("raw_interaction_create")
    async def _on_raw_interaction(self, event: RawGatewayEvent) -> None:
        data = event.data or {}
        if is_entry_point_interaction(data):
            await self._launch(data, followup=True)
        elif is_launch_button_interaction(data):
            await self._launch(data, followup=False)

    async def _launch(self, data: dict, *, followup: bool) -> None:
        interaction_id = data.get("id")
        token = data.get("token")
        if not interaction_id or not token:
            return

        # Open the Activity for the user — no default channel message.
        try:
            await self.bot.http.post_initial_response(
                {"type": CALLBACK_LAUNCH_ACTIVITY}, interaction_id, token
            )
        except Exception:
            log.exception("[ActivityLaunch] LAUNCH_ACTIVITY callback failed")
            return  # launch failed; don't post a message about a non-launch

        # Optional entry-point follow-up (off by default; button never follows up).
        if not followup:
            return
        payload = build_launch_message(data)
        if not payload:
            return
        try:
            await self.bot.http.post_followup(payload, self.bot.app.id, token)
        except Exception:
            log.exception("[ActivityLaunch] launch follow-up message failed")


def setup(bot) -> None:
    ActivityLaunch(bot)
