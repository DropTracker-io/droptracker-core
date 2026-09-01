"""The pure half of the ``vc_to_display_*`` voice-channel counters.

Deliberately free of any ``interactions`` import (and of any DB access) so the
unit suite can exercise the two decisions that actually break these counters —
"is this config value a channel?" and "what name do we write?" — without a
Discord client. ``services/channel_names.py`` holds the loop and the channel-type
gate, which genuinely need the library.
"""

from typing import Optional

# Discord caps channel names at 100 characters and rejects the whole edit when a
# name runs over, so an over-long template would freeze the counter every cycle.
CHANNEL_NAME_MAX = 100


def resolve_channel_id(config_value) -> Optional[int]:
    """The snowflake in a ``vc_to_display_*`` config, or None if there isn't one.

    Three kinds of junk reach this column, and each used to cost a Discord API
    call (or a traceback) every ten minutes:

    * ``''`` / ``None`` — never configured.
    * ``'0'`` — the legacy "unset" sentinel. Truthy as a *string*, so the old
      code happily called ``fetch_channel('0')`` for sixteen groups every pass.
    * free text like ``'Cage'`` — the website's picker degrades to a plain text
      box when a guild has no cached voice channels, and someone typed the
      channel's *name*. interactions.py then raised ``ID (snowflake) should
      represent int`` for group 74 on every cycle.

    Returning None for all three keeps the loop from spending a request to
    rediscover that an unconfigured setting is unconfigured.
    """
    if config_value is None:
        return None
    raw = str(config_value).strip()
    if not raw.isdigit():
        return None
    channel_id = int(raw)
    return channel_id or None


def render_channel_name(template: str, default_template: str, value_key: str, values: dict) -> str:
    """Fill ``template``'s placeholders, guaranteeing the number survives.

    A template that omits its own value placeholder is the single most common
    way these counters "stop working": ``str.replace`` finds nothing, the name
    renders as the static prefix, and the bot then rewrites that identical
    prefix every ten minutes. From the outside it is indistinguishable from a
    dead updater — no error, no log line, and nothing in the guild audit log
    after the first write, because Discord does not record a no-op rename.

    Group 30 typed their channel's own name (``💰⥐Da-Loot:``) into the template
    box, and before that ``{💰⥐Da Loot:}`` — braces around the whole label,
    reading the docs' placeholder syntax as "wrap your text in these".

    So when ``value_key`` is absent from what the group wrote, append the value
    instead of dropping it. They keep the prefix they wanted and get a counter
    that counts, which is what they were asking for either way.
    """
    if not template:
        template = default_template
    rendered = template
    for key, replacement in values.items():
        rendered = rendered.replace(key, replacement)
    if value_key not in template:
        rendered = f"{rendered.rstrip()} {values[value_key]}".strip()
    return rendered[:CHANNEL_NAME_MAX]
