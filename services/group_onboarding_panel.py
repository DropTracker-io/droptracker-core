"""Discord-native group onboarding + configuration (`/group-setup`).

The whole "new clan joins DropTracker" journey without leaving Discord:

- **Bot added to a server** → welcome card in the system channel (owner DM
  fallback) with a *Set up DropTracker* button. Requires the GUILDS intent
  (added alongside this module — the old services/bot_state.py listener was
  dead code without it).
- **Wizard** (no group yet): permission health-check → WiseOldMan group link
  (modal, accepts an id or a pasted URL, live preview) → group name (modal,
  prefilled from WOM) → creation through the same shared
  ``db.group_creation.create_web_group`` service the website uses → channel
  pickers (native ChannelSelectMenu, each choice permission-probed on the
  spot) → notification essentials → done.
- **Config panel** (group exists): category pages generated from
  ``web_api.config_registry`` — booleans as one multi-select, channels as
  ChannelSelects, numbers/text as modals, enums as selects. A new registry
  key shows up here automatically.

Conventions follow services/player_settings_panel.py: ``gset:`` custom_id
prefix, all state in the custom_id (persistent across restarts), one
short-lived Session per interaction, panels are ephemeral. Config writes go
through services/group_config_writer (registry validation + AuditLog +
cache invalidation).
"""
from __future__ import annotations

import re

import interactions
from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    ChannelSelectMenu,
    ChannelType,
    ComponentContext,
    Extension,
    Modal,
    Permissions,
    ShortText,
    SlashContext,
    StringSelectMenu,
    StringSelectOption,
    listen,
    slash_command,
)
from interactions.api.events import Component, GuildJoin, ModalCompletion

from db.app_logger import AppLogger
from db.models import Group, Guild, Session, User
from services.guild_permissions import (
    bot_guild_permission_report,
    missing_channel_perms,
    render_checklist,
)
from services.group_config_writer import (
    get_group_config_values,
    set_group_config,
    validate_updates,
)

app_logger = AppLogger()

PREFIX = "gset:"
SAVED = "✅ **Saved.**\n"
WEBSITE = "https://www.droptracker.io"
WOM_URL_RE = re.compile(r"wiseoldman\.net/groups/(\d+)", re.IGNORECASE)

# Registry keys the panel must never expose: secrets, message-id slots the
# bot itself writes, and group_name — a rename must go through
# db/group_rename.py (the web PATCH does), never a raw config row.
HIDDEN_KEYS = {
    "export_api_key",
    "wom_verification_code",
    "lootboard_message_id",
    "clan_log_message_id",
    "group_name",
}

# The wizard's "essentials" toggle page — the notification switches a brand
# new group cares about on day one. Everything else lives in the full panel.
ESSENTIAL_TOGGLES = (
    "notify_clogs", "notify_cas", "notify_pets", "notify_pbs",
    "notify_quests", "notify_deaths", "notify_diaries",
    "only_send_messages_with_images", "send_stacks_of_items",
)


def parse_wom_input(raw: str):
    """A WOM group id from a bare number or a pasted wiseoldman.net URL —
    mirrors the web wizard's parseWomId. None when unparseable."""
    raw = (raw or "").strip()
    m = WOM_URL_RE.search(raw)
    if m:
        return int(m.group(1))
    digits = raw.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def parse_gp(raw: str):
    """GP amount with 250k/5m/1b shorthand → int, or None."""
    cleaned = (raw or "").replace(",", "").replace(" ", "").strip().lower()
    mult = 1
    if cleaned.endswith("k"):
        mult, cleaned = 1_000, cleaned[:-1]
    elif cleaned.endswith("m"):
        mult, cleaned = 1_000_000, cleaned[:-1]
    elif cleaned.endswith("b"):
        mult, cleaned = 1_000_000_000, cleaned[:-1]
    try:
        value = int(float(cleaned) * mult)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def stored_bool(value: str, default) -> bool:
    """Effective boolean of a stored config string ('' = unset → default)."""
    if value == "" or value is None:
        return bool(default)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ── DB lookups (short-lived sessions) ────────────────────────────────────────

def group_for_guild(guild_id):
    """(group_id, group_name) linked to this Discord server, or None."""
    s = Session()
    try:
        row = (s.query(Group.group_id, Group.group_name)
               .join(Guild, Guild.group_id == Group.group_id)
               .filter(Guild.guild_id == str(guild_id)).first())
        return (int(row[0]), row[1]) if row else None
    finally:
        s.close()


def upsert_guild_row(guild_id) -> None:
    from datetime import datetime

    s = Session()
    try:
        if not s.query(Guild).filter(Guild.guild_id == str(guild_id)).first():
            s.add(Guild(guild_id=str(guild_id), date_added=datetime.now()))
            s.commit()
    finally:
        s.close()


def _user_id_for_discord(discord_id):
    s = Session()
    try:
        row = s.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
        return int(row[0]) if row else None
    finally:
        s.close()


async def _authorized(ctx, group_id=None) -> bool:
    """ADMINISTRATOR in the server, or (for an existing group) on the
    group's authed_users list."""
    from commands.utils import is_admin, is_user_authorized

    if await is_admin(ctx):
        return True
    if group_id is None:
        return False
    s = Session()
    try:
        group = s.query(Group).filter(Group.group_id == group_id).first()
        uid = (s.query(User.user_id)
               .filter(User.discord_id == str(ctx.author.id)).first())
        return bool(group and uid and is_user_authorized(int(uid[0]), group))
    finally:
        s.close()


# ── Registry access ──────────────────────────────────────────────────────────

def _fields():
    from web_api.config_registry import GROUP_CONFIG_FIELDS

    return [f for f in GROUP_CONFIG_FIELDS
            if f["key"] not in HIDDEN_KEYS and f.get("type") != "password"]


def _categories():
    from web_api.config_registry import CONFIG_CATEGORIES

    keys_in_use = {f.get("category") for f in _fields()}
    return [c for c in CONFIG_CATEGORIES if c["key"] in keys_in_use]


def category_fields(cat_key: str):
    return [f for f in _fields() if f.get("category") == cat_key]


def _field(key: str):
    from web_api.config_registry import get_config_field

    return get_config_field(key)


def _short(value: str, limit: int = 60) -> str:
    value = value if value not in (None, "") else "—"
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ── Panel builders ───────────────────────────────────────────────────────────

def _back(custom_id: str = f"{PREFIX}home") -> Button:
    return Button(style=ButtonStyle.SECONDARY, label="← Back", custom_id=custom_id)


async def build_home(bot, guild_id, saved: str = ""):
    """Entry panel: config home when a group exists, wizard start otherwise."""
    linked = group_for_guild(guild_id)
    if linked:
        return build_config_home(linked, saved=saved)
    report = await bot_guild_permission_report(bot, guild_id)
    content = (
        "## 🏰 Set up DropTracker for this server\n"
        "I'll walk you through it right here — no website needed.\n\n"
        "**First, my permissions in this server:**\n"
        + render_checklist(report)
        + "\n\n**You'll need:** a [WiseOldMan](https://wiseoldman.net) group "
        "for your clan (I sync members from it). Have its group id or page "
        "URL ready."
    )
    rows = [ActionRow(
        Button(style=ButtonStyle.SUCCESS, label="Link your WiseOldMan group",
               emoji="🔗", custom_id=f"{PREFIX}wom"),
        Button(style=ButtonStyle.SECONDARY, label="Re-check permissions",
               custom_id=f"{PREFIX}home"),
        Button(style=ButtonStyle.SECONDARY, label="What is DropTracker?",
               custom_id="clan_setup_info"),
    )]
    return content, rows


def build_config_home(linked, saved: str = ""):
    group_id, group_name = linked
    cats = _categories()
    content = (
        f"{saved}## ⚙️ {group_name} — group settings\n"
        f"Pick a category to view or change its settings. Every change "
        f"saves instantly and is audit-logged. (Also on "
        f"[the website]({WEBSITE}/groups/{group_id}/settings).)"
    )
    select = StringSelectMenu(
        *[StringSelectOption(label=c["label"], value=c["key"]) for c in cats],
        placeholder="Choose a settings category…",
        custom_id=f"{PREFIX}cfg:cat:{group_id}",
    )
    return content, [ActionRow(select)]


def build_category_page(group_id: int, cat_key: str, saved: str = ""):
    fields = category_fields(cat_key)
    cat_label = next((c["label"] for c in _categories() if c["key"] == cat_key), cat_key)
    bools = [f for f in fields if f.get("type") == "boolean"]
    others = [f for f in fields if f.get("type") != "boolean"]
    current = get_group_config_values(group_id, [f["key"] for f in fields])

    content = f"{saved}## ⚙️ {cat_label}\n"
    rows = []
    if bools:
        content += "Tick the switches you want **on**; changes save when you close the menu.\n"
        options = [
            StringSelectOption(
                label=f["label"][:100], value=f["key"],
                description=(f.get("help") or "")[:100] or None,
                default=stored_bool(current.get(f["key"], ""), f.get("default")),
            )
            for f in bools[:25]
        ]
        rows.append(ActionRow(StringSelectMenu(
            *options, placeholder="Toggles — select = on…",
            min_values=0, max_values=len(options),
            custom_id=f"{PREFIX}cfg:bools:{cat_key}:{group_id}",
        )))
    if others:
        options = [
            StringSelectOption(
                label=f["label"][:100], value=f["key"],
                description=_short(current.get(f["key"], ""), 100),
            )
            for f in others[:25]
        ]
        rows.append(ActionRow(StringSelectMenu(
            *options, placeholder="Edit a setting…",
            custom_id=f"{PREFIX}cfg:field:{cat_key}:{group_id}",
        )))
    rows.append(ActionRow(_back()))
    return content, rows


def build_channel_editor(group_id: int, key: str, cat_key: str, note: str = ""):
    field = _field(key)
    current = get_group_config_values(group_id, [key]).get(key, "")
    content = (
        f"{note}## 📺 {field['label']}\n"
        + (f"*{field.get('help')}*\n" if field.get("help") else "")
        + f"Current: {('<#' + current + '>') if current else '*not set*'}"
    )
    rows = [
        ActionRow(ChannelSelectMenu(
            channel_types=[ChannelType.GUILD_TEXT, ChannelType.GUILD_NEWS],
            placeholder="Pick a channel…",
            custom_id=f"{PREFIX}cfg:ch:{cat_key}:{group_id}:{key}",
        )),
        ActionRow(
            _back(f"{PREFIX}cfg:page:{cat_key}:{group_id}"),
            Button(style=ButtonStyle.DANGER, label="Clear",
                   custom_id=f"{PREFIX}cfg:clear:{cat_key}:{group_id}:{key}",
                   disabled=not current),
        ),
    ]
    return content, rows


def build_select_editor(group_id: int, key: str, cat_key: str):
    field = _field(key)
    current = get_group_config_values(group_id, [key]).get(key, "") or str(field.get("default") or "")
    content = (f"## ⚙️ {field['label']}\n"
               + (f"*{field.get('help')}*" if field.get("help") else ""))
    options = [
        StringSelectOption(label=str(opt)[:100], value=str(opt),
                           default=(str(opt) == current))
        for opt in (field.get("options") or [])[:25]
    ]
    rows = [
        ActionRow(StringSelectMenu(
            *options, placeholder="Choose…",
            custom_id=f"{PREFIX}cfg:sel:{cat_key}:{group_id}:{key}")),
        ActionRow(_back(f"{PREFIX}cfg:page:{cat_key}:{group_id}")),
    ]
    return content, rows


def build_value_modal(group_id: int, key: str, cat_key: str) -> Modal:
    field = _field(key)
    current = get_group_config_values(group_id, [key]).get(key, "")
    placeholder = str(field.get("default")) if field.get("default") not in (None, "") else None
    return Modal(
        ShortText(
            label=field["label"][:45],
            custom_id="value",
            value=current or None,
            placeholder=(placeholder or "")[:100] or None,
            required=False,
        ),
        title=field["label"][:45],
        custom_id=f"{PREFIX}m:cfg:{cat_key}:{group_id}:{key}",
    )


# ── Wizard builders ──────────────────────────────────────────────────────────

def build_wom_preview(wom_id: int, name: str, member_count) -> tuple:
    content = (
        "## 🔗 WiseOldMan group found\n"
        f"**{name}** — {member_count or '?'} members "
        f"([view](https://wiseoldman.net/groups/{wom_id}))\n\n"
        "Is this your clan?"
    )
    rows = [ActionRow(
        Button(style=ButtonStyle.SUCCESS, label="Yes — name my group",
               custom_id=f"{PREFIX}name:{wom_id}"),
        Button(style=ButtonStyle.SECONDARY, label="Re-enter",
               custom_id=f"{PREFIX}wom"),
        _back(),
    )]
    return content, rows


def build_channels_step(group_id: int, saved: str = ""):
    current = get_group_config_values(
        group_id, ["channel_id_to_post_loot", "lootboard_channel_id"])
    def _fmt(cid):
        return f"<#{cid}>" if cid else "*not set*"
    content = (
        f"{saved}## 📺 Where should I post?\n"
        f"**Drop notifications** — big drops, achievements: "
        f"{_fmt(current['channel_id_to_post_loot'])}\n"
        f"**Lootboard** — the auto-updating loot leaderboard image: "
        f"{_fmt(current['lootboard_channel_id'])}\n\n"
        "Pick channels below (I check my permissions in each as you choose). "
        "You can set per-type channels later in the settings panel."
    )
    rows = [
        ActionRow(ChannelSelectMenu(
            channel_types=[ChannelType.GUILD_TEXT, ChannelType.GUILD_NEWS],
            placeholder="Drop notifications channel…",
            custom_id=f"{PREFIX}wch:{group_id}:channel_id_to_post_loot",
        )),
        ActionRow(ChannelSelectMenu(
            channel_types=[ChannelType.GUILD_TEXT, ChannelType.GUILD_NEWS],
            placeholder="Lootboard channel…",
            custom_id=f"{PREFIX}wch:{group_id}:lootboard_channel_id",
        )),
        ActionRow(Button(style=ButtonStyle.SUCCESS, label="Continue",
                         custom_id=f"{PREFIX}ess:{group_id}")),
    ]
    return content, rows


def build_essentials_step(group_id: int, saved: str = ""):
    keys = list(ESSENTIAL_TOGGLES) + ["minimum_value_to_notify"]
    current = get_group_config_values(group_id, keys)
    try:
        min_value = int(current.get("minimum_value_to_notify") or 0) or 2_500_000
    except ValueError:
        min_value = 2_500_000
    content = (
        f"{saved}## 🔔 Notification essentials\n"
        f"Minimum drop value to announce: **{min_value:,} GP**\n"
        "Tick what you want announced; everything is changeable later."
    )
    options = []
    for key in ESSENTIAL_TOGGLES:
        field = _field(key)
        options.append(StringSelectOption(
            label=field["label"][:100], value=key,
            description=(field.get("help") or "")[:100] or None,
            default=stored_bool(current.get(key, ""), field.get("default")),
        ))
    rows = [
        ActionRow(StringSelectMenu(
            *options, placeholder="Announcement toggles — select = on…",
            min_values=0, max_values=len(options),
            custom_id=f"{PREFIX}ess:set:{group_id}",
        )),
        ActionRow(
            Button(style=ButtonStyle.SECONDARY, label="Minimum drop value…",
                   emoji="💰", custom_id=f"{PREFIX}ess:minval:{group_id}"),
            Button(style=ButtonStyle.SUCCESS, label="Finish setup",
                   custom_id=f"{PREFIX}finish:{group_id}"),
        ),
    ]
    return content, rows


def build_finish_step(group_id: int):
    content = (
        "## 🎉 Your group is live!\n"
        "**Next steps for your members:**\n"
        "1. Install the **DropTracker** plugin from the RuneLite plugin hub.\n"
        "2. Claim their RSN with `/claim-rsn` (links their drops to Discord).\n"
        "3. Watch the drops roll in.\n\n"
        f"Run `/group-setup` any time to change settings, or use "
        f"[the website]({WEBSITE}/groups/{group_id}/settings) for advanced "
        f"options (events, points, embeds, premium)."
    )
    rows = [ActionRow(
        Button(style=ButtonStyle.SECONDARY, label="Open group settings",
               custom_id=f"{PREFIX}home"),
        Button(style=ButtonStyle.LINK, label="Your group page",
               url=f"{WEBSITE}/groups/{group_id}"),
    )]
    return content, rows


# ── Extension ────────────────────────────────────────────────────────────────

class GroupOnboardingPanel(Extension):
    @slash_command(
        name="group-setup",
        description="Set up or configure your clan's DropTracker group — right here in Discord",
        default_member_permissions=Permissions.ADMINISTRATOR,
        dm_permission=False,
    )
    async def group_setup_cmd(self, ctx: SlashContext):
        if not ctx.guild_id:
            await ctx.send("Run this inside your clan's Discord server.", ephemeral=True)
            return
        linked = group_for_guild(ctx.guild_id)
        if not await _authorized(ctx, linked[0] if linked else None):
            await ctx.send("You need **Administrator** here (or to be on the "
                           "group's authorized list) to configure DropTracker.",
                           ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        content, rows = await build_home(self.bot, ctx.guild_id)
        await ctx.send(content, components=rows, ephemeral=True)

    # ── Welcome on join (needs the GUILDS intent) ────────────────────────────
    @listen(GuildJoin)
    async def on_guild_join(self, event: GuildJoin):
        # GUILD_CREATE also fires for every guild at startup; only greet
        # genuinely new servers (no Guild row yet).
        try:
            guild = event.guild
            s = Session()
            try:
                known = s.query(Guild).filter(
                    Guild.guild_id == str(guild.id)).first() is not None
            finally:
                s.close()
            if known:
                return
            upsert_guild_row(guild.id)
            content = (
                "## 👋 DropTracker is here!\n"
                "Track your clan's drops, achievements and events — with "
                "notifications, lootboards and leaderboards in this server.\n\n"
                "An **admin** can set everything up in about two minutes, "
                "without leaving Discord:"
            )
            rows = [ActionRow(
                Button(style=ButtonStyle.SUCCESS, label="Set up DropTracker",
                       emoji="🏰", custom_id=f"{PREFIX}begin"),
                Button(style=ButtonStyle.SECONDARY, label="What is DropTracker?",
                       custom_id="clan_setup_info"),
            )]
            channel = getattr(guild, "system_channel", None)
            if channel is not None:
                try:
                    await channel.send(content, components=rows)
                    return
                except Exception:
                    pass
            owner = await guild.fetch_owner()
            if owner:
                await owner.send(content, components=rows)
        except Exception as e:
            app_logger.log(log_type="error",
                           data=f"guild-join welcome failed: {e}",
                           app_name="core", description="group_onboarding")

    # ── Component router ─────────────────────────────────────────────────────
    @listen(Component)
    async def on_component(self, event: Component):
        ctx: ComponentContext = event.ctx
        cid = ctx.custom_id or ""
        if not cid.startswith(PREFIX):
            return
        try:
            await self._route(ctx, cid[len(PREFIX):])
        except Exception as e:
            app_logger.log(log_type="error",
                           data=f"group-setup panel failed on {cid}: {e}",
                           app_name="core", description="group_onboarding")
            try:
                await ctx.send("Something went wrong — run `/group-setup` again.",
                               ephemeral=True)
            except Exception:
                pass

    async def _route(self, ctx: ComponentContext, action: str):
        if not ctx.guild_id:
            await ctx.send("This panel only works inside a server.", ephemeral=True)
            return
        linked = group_for_guild(ctx.guild_id)
        if not await _authorized(ctx, linked[0] if linked else None):
            await ctx.send("You need **Administrator** here (or to be on the "
                           "group's authorized list) to use this.", ephemeral=True)
            return

        # The public welcome card's button opens a fresh EPHEMERAL panel;
        # everything else edits the ephemeral panel in place.
        if action == "begin":
            content, rows = await build_home(self.bot, ctx.guild_id)
            await ctx.send(content, components=rows, ephemeral=True)
            return

        if action == "home":
            content, rows = await build_home(self.bot, ctx.guild_id)

        elif action == "wom":
            await ctx.send_modal(Modal(
                ShortText(label="WOM group id or page URL", custom_id="wom",
                          placeholder="e.g. 1234 or wiseoldman.net/groups/1234",
                          required=True),
                title="Link your WiseOldMan group",
                custom_id=f"{PREFIX}m:wom",
            ))
            return

        elif action.startswith("name:"):
            wom_id = int(action.split(":", 1)[1])
            from utils.wiseoldman import check_group_by_id

            name, _count, _members = await check_group_by_id(wom_id)
            await ctx.send_modal(Modal(
                ShortText(label="Group name (shown everywhere)",
                          custom_id="name", value=(name or "")[:30],
                          max_length=30, required=True),
                title="Name your DropTracker group",
                custom_id=f"{PREFIX}m:name:{wom_id}",
            ))
            return

        elif action.startswith("wch:"):
            _, gid, key = action.split(":", 2)
            note = await self._save_channel(ctx, int(gid), key)
            content, rows = build_channels_step(int(gid), saved=note)

        elif action.startswith("ess:set:"):
            gid = int(action.rsplit(":", 1)[1])
            updates = {k: (k in set(ctx.values)) for k in ESSENTIAL_TOGGLES}
            set_group_config(gid, validate_updates(updates),
                             actor_discord_id=ctx.author.id)
            content, rows = build_essentials_step(gid, saved=SAVED)

        elif action.startswith("ess:minval:"):
            gid = int(action.rsplit(":", 1)[1])
            await ctx.send_modal(Modal(
                ShortText(label="Minimum drop value (GP)", custom_id="value",
                          placeholder="e.g. 2500000 or 2.5m", required=True),
                title="Minimum drop value",
                custom_id=f"{PREFIX}m:minval:{gid}",
            ))
            return

        elif action.startswith("ess:"):
            content, rows = build_essentials_step(int(action.rsplit(":", 1)[1]))

        elif action.startswith("finish:"):
            content, rows = build_finish_step(int(action.rsplit(":", 1)[1]))

        # ── Config panel ────────────────────────────────────────────────────
        elif action.startswith("cfg:cat:"):
            gid = int(action.rsplit(":", 1)[1])
            content, rows = build_category_page(gid, ctx.values[0])

        elif action.startswith("cfg:page:"):
            _, _, cat_key, gid = action.split(":", 3)
            content, rows = build_category_page(int(gid), cat_key)

        elif action.startswith("cfg:bools:"):
            _, _, cat_key, gid = action.split(":", 3)
            gid = int(gid)
            selected = set(ctx.values)
            updates = {f["key"]: (f["key"] in selected)
                       for f in category_fields(cat_key)
                       if f.get("type") == "boolean"}
            set_group_config(gid, validate_updates(updates),
                             actor_discord_id=ctx.author.id)
            content, rows = build_category_page(gid, cat_key, saved=SAVED)

        elif action.startswith("cfg:field:"):
            _, _, cat_key, gid = action.split(":", 3)
            gid, key = int(gid), ctx.values[0]
            field = _field(key)
            ftype = field.get("type")
            if ftype == "channel":
                content, rows = build_channel_editor(gid, key, cat_key)
            elif ftype == "select":
                content, rows = build_select_editor(gid, key, cat_key)
            else:  # int / string / text / csv / bosslist / boardstyle
                await ctx.send_modal(build_value_modal(gid, key, cat_key))
                return

        elif action.startswith("cfg:ch:"):
            _, _, cat_key, gid, key = action.split(":", 4)
            note = await self._save_channel(ctx, int(gid), key)
            content, rows = build_category_page(int(gid), cat_key, saved=note)

        elif action.startswith("cfg:clear:"):
            _, _, cat_key, gid, key = action.split(":", 4)
            set_group_config(int(gid), {key: ""}, actor_discord_id=ctx.author.id)
            content, rows = build_category_page(int(gid), cat_key, saved=SAVED)

        elif action.startswith("cfg:sel:"):
            _, _, cat_key, gid, key = action.split(":", 4)
            set_group_config(int(gid), validate_updates({key: ctx.values[0]}),
                             actor_discord_id=ctx.author.id)
            content, rows = build_category_page(int(gid), cat_key, saved=SAVED)

        else:
            return
        await ctx.edit_origin(content=content, components=rows)

    async def _save_channel(self, ctx: ComponentContext, group_id: int,
                            key: str) -> str:
        """Persist a ChannelSelect choice + probe the bot's permissions in
        it. Returns the saved/warning note for the re-render."""
        chosen = ctx.values[0]
        channel_id = str(getattr(chosen, "id", chosen))
        set_group_config(group_id, validate_updates({key: channel_id}),
                         actor_discord_id=ctx.author.id)
        channel = chosen if hasattr(chosen, "permissions_for") else None
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                channel = None
        missing = await missing_channel_perms(self.bot, channel) if channel is not None else []
        if missing:
            return ("⚠️ **Saved, but I can't post there yet** — grant me "
                    + ", ".join(f"**{m}**" for m in missing)
                    + f" in <#{channel_id}>.\n")
        return f"✅ **Saved** — I can post in <#{channel_id}>.\n"

    # ── Modal completions ────────────────────────────────────────────────────
    @listen(ModalCompletion)
    async def on_modal(self, event: ModalCompletion):
        ctx = event.ctx
        cid = ctx.custom_id or ""
        if not cid.startswith(f"{PREFIX}m:"):
            return
        try:
            await self._route_modal(ctx, cid[len(PREFIX) + 2:])
        except Exception as e:
            app_logger.log(log_type="error",
                           data=f"group-setup modal failed on {cid}: {e}",
                           app_name="core", description="group_onboarding")
            try:
                await ctx.send("Something went wrong — run `/group-setup` again.",
                               ephemeral=True)
            except Exception:
                pass

    async def _route_modal(self, ctx, action: str):
        if action == "wom":
            wom_id = parse_wom_input(ctx.responses.get("wom"))
            if wom_id is None:
                await ctx.send("That doesn't look like a WOM group id or URL — "
                               "try again via the panel.", ephemeral=True)
                return
            from utils.wiseoldman import check_group_by_id

            name, count, _members = await check_group_by_id(wom_id)
            if not name:
                await ctx.send(f"I couldn't find WOM group `{wom_id}` — "
                               "double-check the id, or create your group at "
                               "https://wiseoldman.net/groups/create first.",
                               ephemeral=True)
                return
            content, rows = build_wom_preview(wom_id, name, count)
            await ctx.send(content, components=rows, ephemeral=True)

        elif action.startswith("name:"):
            wom_id = int(action.split(":", 1)[1])
            group_name = (ctx.responses.get("name") or "").strip()
            from db.group_creation import create_web_group

            result = await create_web_group(
                group_name=group_name, wom_id=wom_id,
                guild_id=str(ctx.guild_id),
                owner_discord_id=str(ctx.author.id),
                owner_username=str(ctx.author.username),
            )
            status = result.get("status")
            if status in ("created", "already_registered"):
                gid = result.get("group_id")
                note = ("✅ **Group created!** Members are syncing from "
                        "WiseOldMan in the background.\n\n")
                content, rows = build_channels_step(int(gid), saved=note)
                await ctx.send(content, components=rows, ephemeral=True)
            else:
                friendly = {
                    "guild_conflict": "This server is already linked to a different group.",
                    "wom_conflict": f"WOM group `{wom_id}` is already registered to another group.",
                    "invalid_name": "Group names must be 1–30 characters.",
                    "invalid_wom": "That WOM id doesn't look right.",
                }.get(status, result.get("message") or "A database error occurred — try again shortly.")
                await ctx.send(f"⚠️ {friendly}", ephemeral=True)

        elif action.startswith("minval:"):
            gid = int(action.split(":", 1)[1])
            value = parse_gp(ctx.responses.get("value"))
            if value is None:
                await ctx.send("That doesn't look like a GP amount — try "
                               "`2500000`, `2.5m` or `500k`.", ephemeral=True)
                return
            set_group_config(gid, validate_updates({"minimum_value_to_notify": value}),
                             actor_discord_id=ctx.author.id)
            await ctx.send(f"✅ Drops worth **{value:,} GP** or more will be "
                           "announced.", ephemeral=True)

        elif action.startswith("cfg:"):
            _, cat_key, gid, key = action.split(":", 3)
            gid = int(gid)
            raw = (ctx.responses.get("value") or "").strip()
            from web_api.config_registry import ConfigValidationError

            try:
                if raw == "":
                    set_group_config(gid, {key: ""}, actor_discord_id=ctx.author.id)
                else:
                    set_group_config(gid, validate_updates({key: raw}),
                                     actor_discord_id=ctx.author.id)
            except (ConfigValidationError, ValueError) as e:
                await ctx.send(f"⚠️ {e}", ephemeral=True)
                return
            field = _field(key)
            await ctx.send(f"✅ **{field['label']}** saved.", ephemeral=True)


def setup(bot):
    GroupOnboardingPanel(bot)
