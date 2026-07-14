"""
Pure logic for the Discord Activity launcher — deliberately free of any
``interactions`` import so it's unit-testable without a live Discord client.

The interactions-dependent shell (raw interaction handling, the Components-V2
card, the Extension, the bot HTTP calls) lives in ``services/activity_launch.py``
and injects its ``post_card`` / ``delete_message`` side effects into
``reconcile_group`` here.
"""
import logging

log = logging.getLogger("interactions")

# Discord numeric constants (absent from interactions.py 5.16 enums).
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
COMMAND_PRIMARY_ENTRY_POINT = 4
CALLBACK_LAUNCH_ACTIVITY = 12
MSG_FLAG_EPHEMERAL = 1 << 6  # 64

# Brand gold, matching the Activity's OSRS palette.
ACCENT = 0xC8A24A

# custom_id of the "Open DropTracker" button on the standing card.
LAUNCH_BUTTON_CUSTOM_ID = "activity_launch_open"

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
    """True iff this is a click on our "Open DropTracker" card button."""
    if not isinstance(data, dict) or data.get("type") != INTERACTION_MESSAGE_COMPONENT:
        return False
    return (data.get("data") or {}).get("custom_id") == LAUNCH_BUTTON_CUSTOM_ID


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
