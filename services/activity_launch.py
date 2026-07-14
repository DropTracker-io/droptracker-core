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
    CALLBACK_CHANNEL_MESSAGE,
    CALLBACK_LAUNCH_ACTIVITY,
    LAUNCH_BUTTON_CUSTOM_ID,
    LAUNCH_INTENT_TTL,
    build_launch_fallback_message,
    build_launch_message,
    intent_key,
    is_entry_point_interaction,
    is_launch_button_interaction,
    launch_intent_from_interaction,
    launch_supported_channel_type,
)

log = logging.getLogger("interactions")


def _record_launch_intent(data: dict) -> None:
    """When a user clicks an event-scoped launch button, remember which event
    so the Activity can open straight to it (claimed via the web_api
    launch-intent endpoint after OAuth). Best-effort — a Redis hiccup just
    means the app opens to its home hub instead of the event."""
    intent = launch_intent_from_interaction(data)
    if not intent:
        return
    user_id, event_id = intent
    try:
        from utils.redis import redis_client

        redis_client.setex(intent_key(user_id), LAUNCH_INTENT_TTL, str(event_id))
    except Exception:
        log.debug("[ActivityLaunch] couldn't stash launch intent", exc_info=True)


def channel_supports_launch(channel) -> bool:
    """Whether an interactions channel object can host a LAUNCH_ACTIVITY
    response (Discord refuses it in threads/announcement channels)."""
    channel_type = getattr(channel, "type", None)
    try:
        channel_type = int(channel_type) if channel_type is not None else None
    except (TypeError, ValueError):
        channel_type = None
    return launch_supported_channel_type(channel_type)


def build_launch_card(supports_launch: bool = True):
    """Components-V2 "Open DropTracker" card. Its button (custom_id
    LAUNCH_BUTTON_CUSTOM_ID) opens the Activity when clicked. Matches the bot's
    other V2 cards (services/components.py). With ``supports_launch`` False
    (the configured channel is a thread/announcement channel, where Discord
    refuses app launches) the button links to the website and the card says
    how to get the in-Discord launcher back."""
    from interactions import ActionRow, Button, ButtonStyle, UnfurledMediaItem
    from interactions.models import (
        ContainerComponent,
        SectionComponent,
        SeparatorComponent,
        TextDisplayComponent,
        ThumbnailComponent,
    )

    if supports_launch:
        cta = (
            "Tap **Open DropTracker** to launch the app. Follow live bingo "
            "boards, browse the leaderboards, and sign up for events without "
            "leaving Discord."
        )
        button = Button(
            label="Open DropTracker",
            style=ButtonStyle.BLURPLE,
            custom_id=LAUNCH_BUTTON_CUSTOM_ID,
        )
    else:
        cta = (
            "Discord can't launch apps inside threads or announcement "
            "channels, so this card links to the website instead. Pick a "
            "regular text channel as your launcher channel to open the app "
            "right here in Discord."
        )
        button = Button(
            label="Open DropTracker on the web",
            style=ButtonStyle.URL,
            url=core.WEBSITE_URL,
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
            TextDisplayComponent(content=cta),
            SeparatorComponent(divider=True),
            ActionRow(button),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content="-# Powered by the [DropTracker](https://www.droptracker.io)"),
        )
    ]


async def _post_card(bot, channel_id):
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return None
        return await channel.send(
            components=build_launch_card(supports_launch=channel_supports_launch(channel))
        )
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
            # Event-scoped buttons carry a deep-link target — stash it before
            # opening so the Activity can claim it on boot.
            _record_launch_intent(data)
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
            # Discord refuses LAUNCH_ACTIVITY in threads/announcement channels
            # (400 "Cannot execute action on this channel type"). The
            # interaction is still unacknowledged, so answer with an ephemeral
            # explainer + web link instead of leaving the user a silent
            # "This interaction failed".
            log.warning(
                "[ActivityLaunch] LAUNCH_ACTIVITY refused — sending web fallback",
                exc_info=True,
            )
            try:
                await self.bot.http.post_initial_response(
                    {
                        "type": CALLBACK_CHANNEL_MESSAGE,
                        "data": build_launch_fallback_message(data),
                    },
                    interaction_id,
                    token,
                )
            except Exception:
                log.exception("[ActivityLaunch] launch fallback message failed")
            return  # launch didn't happen; skip the launch follow-up

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
