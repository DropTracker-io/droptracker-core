"""
Pure logic for the Discord Activity launcher — deliberately free of any
``interactions`` import so it's unit-testable without a live Discord client.

The interactions-dependent shell (raw interaction handling, the Components-V2
card, the Extension, the bot HTTP calls) lives in ``services/activity_launch.py``
and injects its ``post_card`` / ``delete_message`` side effects into
``reconcile_group`` here.
"""
import logging
import os

log = logging.getLogger("interactions")

# Discord numeric constants (absent from interactions.py 5.16 enums).
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
COMMAND_PRIMARY_ENTRY_POINT = 4
CALLBACK_CHANNEL_MESSAGE = 4
CALLBACK_LAUNCH_ACTIVITY = 12
MSG_FLAG_EPHEMERAL = 1 << 6  # 64

# Channel types Discord will actually launch an Activity from: guild text,
# DM, guild voice, group DM. Threads, announcement channels, stages and
# forums reject the LAUNCH_ACTIVITY callback with
# 400 "Cannot execute action on this channel type".
LAUNCH_SUPPORTED_CHANNEL_TYPES = frozenset({0, 1, 2, 3})

WEBSITE_URL = "https://www.droptracker.io"
EVENT_BASE_URL = f"{WEBSITE_URL}/events"  # == services.event_notifications.EVENT_BASE_URL

# Discord application that owns the Activity (the primary bot app).
ACTIVITY_APP_ID = os.environ.get("DISCORD_BOT_CLIENT_ID", "").strip() or "1172933457010245762"


def activity_link_url(event_id=None) -> str:
    """Discord Activity Link — launches the app client-side when clicked, so
    it works from threads/announcement channels where the LAUNCH_ACTIVITY
    interaction callback is refused. An event id rides along as the link's
    ``custom_id`` (surfaced to the app as ``sdk.customId``, format
    ``e:<event_id>``) so the Activity can deep-link to that event."""
    base = f"https://discord.com/activities/{ACTIVITY_APP_ID}"
    if event_id in (None, "", 0):
        return base
    return f"{base}?custom_id=e%3A{event_id}"

# Brand gold, matching the Activity's OSRS palette.
ACCENT = 0xC8A24A

# custom_id of the "Open DropTracker" button on the standing card. Event-scoped
# launch buttons (the "Open in Discord" buttons on event notifications) append
# ``:e:<event_id>`` so the raw handler can record which event to deep-link to.
LAUNCH_BUTTON_CUSTOM_ID = "activity_launch_open"
LAUNCH_EVENT_INFIX = ":e:"

# Deep-link handoff: the raw handler stashes {discord_user_id -> event_id} in
# Redis when a user clicks an event-scoped launch button; the Activity claims it
# after OAuth (web_api GET /events/launch-intent) and opens straight to that
# event. Short TTL — it's a one-shot intent for the launch that just happened.
LAUNCH_INTENT_PREFIX = "dt:activity:launch:"
LAUNCH_INTENT_TTL = 120

# Group-config key a leader sets (in the registry); and the bot-managed row that
# tracks the posted card as "channel_id:message_id" (NOT in the config registry).
CHANNEL_KEY = "activity_launch_channel"
MSG_REF_KEY = "activity_launch_message_id"

# Whether opening the app from the launcher posts any message. Off by default —
# opening should be silent; discoverability comes from the standing channel card.
SEND_LAUNCH_MESSAGE = False
# If re-enabled, whether that message is ephemeral (launcher only) or public.
LAUNCH_MESSAGE_EPHEMERAL = True


def is_entry_point_interaction(data: dict) -> bool:
    """True iff this raw interaction is an Activity entry-point ("launch") command."""
    if not isinstance(data, dict) or data.get("type") != INTERACTION_APPLICATION_COMMAND:
        return False
    return (data.get("data") or {}).get("type") == COMMAND_PRIMARY_ENTRY_POINT


def is_launch_button_interaction(data: dict) -> bool:
    """True iff this is a click on a launch button — the standing "Open
    DropTracker" card button *or* an event-scoped "Open in Discord" button
    (``activity_launch_open:e:<event_id>``) on an event notification."""
    if not isinstance(data, dict) or data.get("type") != INTERACTION_MESSAGE_COMPONENT:
        return False
    custom_id = (data.get("data") or {}).get("custom_id") or ""
    return custom_id == LAUNCH_BUTTON_CUSTOM_ID or custom_id.startswith(
        LAUNCH_BUTTON_CUSTOM_ID + LAUNCH_EVENT_INFIX
    )


def launch_button_custom_id(event_id=None) -> str:
    """The custom_id for a launch button: the bare card button when
    ``event_id`` is None, else the event-scoped deep-link variant."""
    if event_id in (None, "", 0):
        return LAUNCH_BUTTON_CUSTOM_ID
    return f"{LAUNCH_BUTTON_CUSTOM_ID}{LAUNCH_EVENT_INFIX}{event_id}"


def parse_launch_custom_id(custom_id) -> "str | None":
    """The event id encoded in an event-scoped launch button's custom_id, or
    None for the bare card button / anything else."""
    if not isinstance(custom_id, str):
        return None
    prefix = LAUNCH_BUTTON_CUSTOM_ID + LAUNCH_EVENT_INFIX
    if not custom_id.startswith(prefix):
        return None
    event_id = custom_id[len(prefix):].strip()
    return event_id if event_id.isdigit() else None


def interaction_user_id(data: dict) -> "str | None":
    """The clicking user's Discord id from a raw interaction — ``member.user``
    in a guild, top-level ``user`` in a DM."""
    if not isinstance(data, dict):
        return None
    member = data.get("member") or {}
    user = member.get("user") or data.get("user") or {}
    uid = user.get("id")
    return str(uid) if uid else None


def intent_key(discord_user_id) -> str:
    """Redis key for one user's pending launch-intent event id."""
    return f"{LAUNCH_INTENT_PREFIX}{discord_user_id}"


def launch_intent_from_interaction(data: dict) -> "tuple[str, str] | None":
    """``(discord_user_id, event_id)`` to stash for a deep-link launch, or None
    when this launch carries no event (the bare card button) or lacks a user.
    Pure — the shell does the actual Redis write."""
    event_id = parse_launch_custom_id((data.get("data") or {}).get("custom_id"))
    if not event_id:
        return None
    user_id = interaction_user_id(data)
    if not user_id:
        return None
    return user_id, event_id


def interaction_channel_type(data: dict):
    """The channel type from a raw interaction payload (Discord includes a
    partial ``channel`` object on every interaction), or None if absent."""
    if not isinstance(data, dict):
        return None
    channel = data.get("channel")
    if not isinstance(channel, dict):
        return None
    return channel.get("type")


def launch_supported_channel_type(channel_type) -> bool:
    """Whether a Discord channel type supports the LAUNCH_ACTIVITY callback.
    Unknown/None counts as supported — the click-time fallback in the shell
    answers anything Discord actually rejects, so a wrong True here costs
    nothing while a wrong False needlessly hides working launch buttons."""
    if channel_type is None:
        return True
    try:
        return int(channel_type) in LAUNCH_SUPPORTED_CHANNEL_TYPES
    except (TypeError, ValueError):
        return True


def build_launch_fallback_message(data: dict) -> dict:
    """Ephemeral response for a launch Discord refused (unsupported channel
    type — a thread or announcement channel). Offers an Activity Link (a
    client-side launch that DOES work from these channels) plus the web page:
    the event page for an event-scoped button, else the site."""
    event_id = parse_launch_custom_id((data.get("data") or {}).get("custom_id"))
    if event_id:
        web_label, web_url = "View on the web", f"{EVENT_BASE_URL}/{event_id}"
    else:
        web_label, web_url = "Open droptracker.io", WEBSITE_URL
    return {
        "content": (
            "Discord can't launch apps directly from this channel type — "
            "threads and announcement channels don't support it. Use the "
            "**Open DropTracker** link below instead, or view on the web."
        ),
        "flags": MSG_FLAG_EPHEMERAL,
        "components": [
            {
                "type": 1,  # action row
                "components": [
                    {"type": 2, "style": 5, "label": "Open DropTracker",
                     "url": activity_link_url(event_id)},
                    {"type": 2, "style": 5, "label": web_label, "url": web_url},
                ],
            }
        ],
    }


def build_launch_message(data: dict) -> dict | None:
    """Optional message posted when someone opens the app from the launcher.
    Returns None unless SEND_LAUNCH_MESSAGE is on."""
    if not SEND_LAUNCH_MESSAGE:
        return None
    payload: dict = {
        "embeds": [
            {
                "title": "DropTracker is open",
                "description": (
                    "Your clan's live event boards, leaderboards, and your own "
                    "profile are all in here. Others can open it from the app "
                    "tray to join too."
                ),
                "color": ACCENT,
            }
        ]
    }
    if LAUNCH_MESSAGE_EPHEMERAL:
        payload["flags"] = MSG_FLAG_EPHEMERAL
    return payload


def parse_ref(value: str | None) -> tuple[str | None, str | None]:
    """Split a stored "channel_id:message_id" ref into its parts."""
    if value and ":" in value:
        channel_id, _, message_id = value.partition(":")
        return (channel_id or None), (message_id or None)
    return None, None


def clean_channel(value: str | None) -> str | None:
    """Normalize a configured channel id; None when unset/zero/invalid."""
    value = (value or "").strip()
    return value if value.isdigit() and value != "0" else None


async def reconcile_group(session, group_id, channel_id, ref_row, *, post_card, delete_message) -> None:
    """Bring one group's standing card in line with its configured channel.

    ``post_card(channel_id) -> message|None`` and
    ``delete_message(channel_id, message_id)`` are injected by the shell so this
    stays Discord-free and testable. Cheap in steady state — when the card is
    already in the configured channel this returns without any side effect.
    """
    from db.models import GroupConfiguration

    stored_channel, stored_msg = parse_ref(ref_row.config_value if ref_row else None)

    # Channel cleared → remove the card and forget it.
    if not channel_id:
        if stored_channel and stored_msg:
            await delete_message(stored_channel, stored_msg)
        if ref_row is not None:
            session.delete(ref_row)
            session.commit()
        return

    # Already posted in the right channel → nothing to do (no API call).
    if stored_channel == str(channel_id) and stored_msg:
        return

    # New, or the channel changed → drop the stale card, post fresh.
    if stored_channel and stored_msg and stored_channel != str(channel_id):
        await delete_message(stored_channel, stored_msg)

    message = await post_card(channel_id)
    if message is None:
        return
    new_ref = f"{channel_id}:{message.id}"
    if ref_row is not None:
        ref_row.config_value = new_ref
    else:
        session.add(
            GroupConfiguration(group_id=group_id, config_key=MSG_REF_KEY, config_value=new_ref)
        )
    session.commit()


async def reconcile_all(session_factory, *, post_card, delete_message) -> None:
    """Sweep every group with a launcher card configured or posted, reconciling
    each. DB reads only in steady state; API calls only when a card must move."""
    from db.models import GroupConfiguration

    session = session_factory()
    try:
        channel_rows = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.config_key == CHANNEL_KEY)
            .all()
        )
        ref_rows = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.config_key == MSG_REF_KEY)
            .all()
        )
        channels = {r.group_id: clean_channel(r.config_value) for r in channel_rows}
        refs = {r.group_id: r for r in ref_rows}
        for group_id in set(channels) | set(refs):
            try:
                await reconcile_group(
                    session,
                    group_id,
                    channels.get(group_id),
                    refs.get(group_id),
                    post_card=post_card,
                    delete_message=delete_message,
                )
            except Exception:
                log.exception("[ActivityLaunch] reconcile failed for group %s", group_id)
    finally:
        session.close()
