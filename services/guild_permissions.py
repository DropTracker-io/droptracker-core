"""Bot self-permission introspection for a guild / channel.

Nothing in the codebase proactively checks what the bot is allowed to do —
every permission problem today is discovered reactively as a caught
``Forbidden`` after something failed silently for the group. The onboarding
panel inverts that: it shows a ✅/⚠️ checklist up front and re-probes each
channel as it is configured, so "the bot can't actually post there" is caught
at setup time, not at the first missed drop.

Pure helpers where possible (checklist shaping is unit-testable); the two
``async`` functions only fetch via REST — no reliance on gateway caches, so
they work the moment the bot is invited.
"""
from __future__ import annotations

from interactions import Permissions

# What the bot needs anywhere it posts notifications/lootboards. Missing any
# of these breaks core functionality in a configured channel.
REQUIRED_POST_PERMS = (
    (Permissions.VIEW_CHANNEL, "View Channel"),
    (Permissions.SEND_MESSAGES, "Send Messages"),
    (Permissions.EMBED_LINKS, "Embed Links"),
    (Permissions.ATTACH_FILES, "Attach Files"),
    (Permissions.READ_MESSAGE_HISTORY, "Read Message History"),
)

# Needed only by specific features — absence is a note, not a failure.
OPTIONAL_PERMS = (
    (Permissions.MANAGE_CHANNELS, "Manage Channels",
     "event team channels & voice-channel stat displays"),
    (Permissions.MANAGE_ROLES, "Manage Roles",
     "event team roles & the news opt-in role"),
)

# The invite bitfield: everything above, in one place. web_api/routes/meta.py
# and services/components.py both build invite URLs from this so the three
# invite surfaces can never drift (they used to: one asked for Administrator,
# one asked for nothing).
INVITE_PERMISSIONS = 0
for _flag, _label in REQUIRED_POST_PERMS:
    INVITE_PERMISSIONS |= int(_flag)
for _flag, _label, _why in OPTIONAL_PERMS:
    INVITE_PERMISSIONS |= int(_flag)


def permission_checklist(perms: int) -> dict:
    """Shape one effective-permission bitfield into the report the panel
    renders: {"ok": bool, "required": [(label, bool)], "optional":
    [(label, bool, why)], "admin": bool}."""
    perms = int(perms or 0)
    admin = bool(perms & int(Permissions.ADMINISTRATOR))
    required = [(label, admin or bool(perms & int(flag)))
                for flag, label in REQUIRED_POST_PERMS]
    optional = [(label, admin or bool(perms & int(flag)), why)
                for flag, label, why in OPTIONAL_PERMS]
    return {
        "ok": all(ok for _l, ok in required),
        "required": required,
        "optional": optional,
        "admin": admin,
    }


def render_checklist(report: dict) -> str:
    """The checklist as Discord markdown lines."""
    if report["admin"]:
        return "✅ I have **Administrator** here — every feature can run."
    lines = []
    for label, ok in report["required"]:
        lines.append(f"{'✅' if ok else '⚠️'} **{label}**"
                     + ("" if ok else " — needed to post notifications"))
    for label, ok, why in report["optional"]:
        lines.append(f"{'✅' if ok else '▫️'} {label} — *optional: {why}*")
    if not report["ok"]:
        lines.append(
            "\nGrant the missing permissions to the **DropTracker** role in "
            "Server Settings → Roles (or re-invite the bot), then press "
            "**Re-check**.")
    return "\n".join(lines)


async def bot_guild_permission_report(bot, guild_id) -> dict:
    """Effective guild-level permissions of the bot itself, via REST.

    Computed from the bot member's roles against the fetched guild's role
    list — deliberately not ``bot.guilds`` (this bot historically runs
    without relying on the guild cache)."""
    guild = await bot.fetch_guild(guild_id)
    if guild is None:
        return permission_checklist(0)
    member = await guild.fetch_member(bot.user.id)
    if member is None:
        return permission_checklist(0)
    try:
        perms = int(member.guild_permissions)
    except Exception:
        # Fall back to folding role permissions by hand (role cache miss).
        perms = 0
        role_perms = {int(r.id): int(r.permissions) for r in (guild.roles or [])}
        perms |= role_perms.get(int(guild_id), 0)  # @everyone
        for role in (member.roles or []):
            perms |= role_perms.get(int(role.id), 0)
    return permission_checklist(perms)


async def missing_channel_perms(bot, channel) -> list:
    """Labels of REQUIRED_POST_PERMS the bot lacks in one channel, or [] —
    the per-channel probe run right after an admin picks a channel."""
    try:
        guild = await bot.fetch_guild(channel._guild_id if hasattr(channel, "_guild_id") else channel.guild.id)
        member = await guild.fetch_member(bot.user.id)
        perms = int(channel.permissions_for(member))
    except Exception:
        return []  # can't compute — don't block setup on introspection failure
    if perms & int(Permissions.ADMINISTRATOR):
        return []
    return [label for flag, label in REQUIRED_POST_PERMS
            if not perms & int(flag)]
