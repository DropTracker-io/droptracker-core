"""The `/settings` panel — player self-service configuration inside Discord.

Everything here already existed on the website settings page; this panel
mirrors it so a player never *has* to open the site for the common toggles:

- DM notifications (account changes, monthly recap, clan invites, and the
  supporter per-type submission DMs + minimum drop value).
- In-game event notifications (per-RSN type toggles — the same
  ``player_notification_prefs`` rows the website writes, enforced at
  plugin-inbox delivery time).
- Pings & visibility (submission pings in the global/clan servers, and the
  global-listing hide switch).

Design follows :mod:`services.recap_buttons` / :mod:`services.entry_modifier`:
every component is persistent (state travels in the ``custom_id``), handlers
identify the presser by Discord id and only ever edit rows belonging to that
user, and each interaction uses its own short-lived Session. Storage semantics
deliberately mirror ``web_api/routes/me.py`` (the website settings API) so the
two surfaces can never disagree about what a row means.
"""
from __future__ import annotations

import json

from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    ComponentContext,
    Extension,
    Modal,
    ShortText,
    SlashContext,
    StringSelectMenu,
    StringSelectOption,
    listen,
    slash_command,
)
from interactions.api.events import Component, ModalCompletion

from db.app_logger import AppLogger
from db.models import (
    Player,
    PlayerNotificationPrefs,
    Session,
    User,
    UserConfiguration,
)

app_logger = AppLogger()

PREFIX = "pset:"
SETTINGS_URL = "https://www.droptracker.io/settings"

# ── DM notification keys (mirrors web_api/routes/me.py) ──────────────────────
# (key, label, description, default_on). default_on: an absent row means
# enabled — the opt-OUT shape (me.py _CONFIG_SETTINGS_DEFAULT_ON).
DM_OPTIONS = (
    ("dm_account_changes", "Account name changes", "When a claimed RSN changes names", False),
    ("dm_monthly_recap", "Monthly recap", "Your recap card on the 1st of each month", False),
    ("dm_clan_invites", "Clan event invites", "When another clan challenges yours", True),
    ("dm_drops", "Drops (supporter)", "DM me my tracked drops", False),
    ("dm_pbs", "Personal bests (supporter)", "DM me new personal bests", False),
    ("dm_cas", "Combat achievements (supporter)", "DM me combat achievement unlocks", False),
    ("dm_clogs", "Collection log (supporter)", "DM me new collection log slots", False),
    ("dm_pets", "Pets (supporter)", "DM me pet drops", False),
    ("dm_quests", "Quests (supporter)", "DM me quest completions", False),
    ("dm_deaths", "Deaths (supporter)", "DM me my deaths", False),
    ("dm_diaries", "Achievement diaries (supporter)", "DM me diary completions", False),
    ("dm_levels", "Level ups (supporter)", "DM me level-ups", False),
)
DM_MIN_VALUE_KEY = "dm_min_value"
DM_MIN_VALUE_MAX = 2_147_483_647

# ── In-game event notification types ─────────────────────────────────────────
# Keys come from the delivery-time source of truth; labels mirror
# web_api/routes/me.py _EVENT_PREF_LABELS (private there, so kept in sync by
# hand). event_task_progress is deliberately absent — client-toggle-only.
EVENT_PREF_LABELS = {
    "event_completion": "Task completions",
    "event_line": "Bingo line completions",
    "event_blackout": "Bingo blackouts",
    "event_board_turn": "Board rolls & moves",
    "event_board_roll_prompt": "Roll-the-dice prompts",
    "event_lead_change": "Lead changes",
    "event_started": "Event started",
    "event_ended": "Event ended",
    "event_window_opened": "Scoring window opens",
    "event_window_closed": "Scoring window closes",
}


def _event_pref_types() -> tuple:
    from services.plugin_notifications import WEB_PREF_TYPES

    return WEB_PREF_TYPES


# ── Pure selection <-> state mapping (unit-tested) ───────────────────────────

def dm_updates_from_selection(selected: set) -> dict:
    """Multi-select submission -> {config_key: "true"/"false"} for every DM
    key (selected = enabled; everything else explicitly disabled, matching the
    website's store-both semantics for these keys)."""
    return {key: ("true" if key in selected else "false")
            for key, _label, _desc, _default in DM_OPTIONS}


def event_prefs_from_selection(selected: set, types) -> str:
    """Multi-select submission -> the ``player_notification_prefs.prefs`` JSON.
    Only explicit ``false`` is persisted (absent = enabled, so future types
    default on) — mirrors PUT /me/players/{id}/notification-prefs."""
    return json.dumps({t: False for t in types if t not in selected})


# ── DB helpers (one short-lived session per interaction) ─────────────────────

def _resolve_user_id(discord_id: str):
    s = Session()
    try:
        row = (s.query(User.user_id)
               .filter(User.discord_id == str(discord_id)).first())
        return int(row[0]) if row else None
    finally:
        s.close()


def _config_map(s, user_id: int) -> dict:
    rows = (s.query(UserConfiguration.config_key, UserConfiguration.config_value)
            .filter(UserConfiguration.user_id == user_id).all())
    return {k: v for k, v in rows}


def _dm_state(user_id: int) -> tuple[dict, int]:
    """({key: enabled}, dm_min_value)."""
    s = Session()
    try:
        cfg = _config_map(s, user_id)
        state = {}
        for key, _label, _desc, default_on in DM_OPTIONS:
            raw = cfg.get(key)
            if raw is None:
                state[key] = default_on
            elif default_on:
                state[key] = str(raw).lower() not in ("false", "0")
            else:
                state[key] = str(raw).lower() in ("true", "1")
        try:
            min_value = int(cfg.get(DM_MIN_VALUE_KEY) or 0)
        except (TypeError, ValueError):
            min_value = 0
        return state, min_value
    finally:
        s.close()


def _set_config_values(user_id: int, updates: dict) -> None:
    s = Session()
    try:
        existing = {
            row.config_key: row
            for row in s.query(UserConfiguration)
            .filter(UserConfiguration.user_id == user_id,
                    UserConfiguration.config_key.in_(list(updates)))
            .all()
        }
        for key, value in updates.items():
            row = existing.get(key)
            if row:
                row.config_value = value
            else:
                s.add(UserConfiguration(user_id=user_id, config_key=key,
                                        config_value=value))
        s.commit()
    finally:
        s.close()


def _players(user_id: int) -> list:
    s = Session()
    try:
        return [
            {"id": p.player_id, "name": p.player_name}
            for p in s.query(Player).filter(Player.user_id == user_id)
            .order_by(Player.player_name.asc()).all()
        ]
    finally:
        s.close()


def _event_prefs_state(player_id: int, types) -> dict:
    s = Session()
    try:
        row = (s.query(PlayerNotificationPrefs)
               .filter(PlayerNotificationPrefs.player_id == player_id).first())
        disabled = set()
        if row is not None:
            try:
                raw = json.loads(row.prefs or "{}")
                disabled = {k for k, v in raw.items() if v is False}
            except (TypeError, ValueError):
                disabled = set()
        return {t: (t not in disabled) for t in types}
    finally:
        s.close()


def _save_event_prefs(user_id: int, player_id: int, prefs_json: str) -> bool:
    """Write one owned account's prefs; False when the account isn't theirs
    (defensive — the select only ever lists their own accounts)."""
    s = Session()
    try:
        player = (s.query(Player)
                  .filter(Player.player_id == player_id,
                          Player.user_id == user_id).first())
        if not player:
            return False
        row = (s.query(PlayerNotificationPrefs)
               .filter(PlayerNotificationPrefs.player_id == player_id).first())
        if row is None:
            row = PlayerNotificationPrefs(player_id=player_id, prefs="{}")
            s.add(row)
        row.prefs = prefs_json
        s.commit()
        return True
    finally:
        s.close()


def _ping_state(user_id: int) -> dict:
    s = Session()
    try:
        user = s.query(User).filter(User.user_id == user_id).first()
        return {
            "global_ping": bool(user and user.global_ping),
            "group_ping": bool(user and user.group_ping),
            "never_ping": bool(user and user.never_ping),
            "hidden": bool(user and user.hidden),
        }
    finally:
        s.close()


def _save_ping_state(user_id: int, selected: set) -> None:
    s = Session()
    try:
        user = s.query(User).filter(User.user_id == user_id).first()
        if not user:
            return
        user.global_ping = "global" in selected
        user.group_ping = "group" in selected
        # The master no-pings override tracks the two toggles: nothing
        # selected reads as "never ping me", either selected clears it.
        user.never_ping = not (user.global_ping or user.group_ping)
        user.hidden = "visible" not in selected
        s.commit()
    finally:
        s.close()


# ── Panel builders ───────────────────────────────────────────────────────────

def _back_button() -> Button:
    return Button(style=ButtonStyle.SECONDARY, label="← Back",
                  custom_id=f"{PREFIX}home")


def build_main_panel(saved: str = "") -> tuple[str, list]:
    content = (
        f"{saved}## ⚙️ DropTracker settings\n"
        "Configure your notifications and privacy right here — no website "
        f"needed. (Everything is also on [the settings page]({SETTINGS_URL}).)"
    )
    row = ActionRow(
        Button(style=ButtonStyle.PRIMARY, label="DM notifications",
               emoji="🔔", custom_id=f"{PREFIX}dms"),
        Button(style=ButtonStyle.PRIMARY, label="In-game notifications",
               emoji="🎮", custom_id=f"{PREFIX}ig"),
        Button(style=ButtonStyle.PRIMARY, label="Pings & visibility",
               emoji="👁️", custom_id=f"{PREFIX}pings"),
    )
    return content, [row]


def build_dms_panel(user_id: int, saved: str = "") -> tuple[str, list]:
    state, min_value = _dm_state(user_id)
    content = (
        f"{saved}## 🔔 DM notifications\n"
        "Tick the messages you want me to DM you. Submission DMs (drops, PBs, "
        "…) need an active **supporter** subscription to actually send — the "
        "choices save either way.\n"
        f"Minimum drop value for drop DMs: **{min_value:,} GP**"
        + ("" if min_value else " (every tracked drop)")
    )
    select = StringSelectMenu(
        *[
            StringSelectOption(label=label, value=key, description=desc,
                               default=state[key])
            for key, label, desc, _default in DM_OPTIONS
        ],
        placeholder="Choose which DMs you receive…",
        min_values=0,
        max_values=len(DM_OPTIONS),
        custom_id=f"{PREFIX}dms:set",
    )
    buttons = ActionRow(
        _back_button(),
        Button(style=ButtonStyle.SECONDARY, label="Minimum drop value…",
               emoji="💰", custom_id=f"{PREFIX}dms:minval"),
    )
    return content, [ActionRow(select), buttons]


def build_ig_panel(user_id: int, player_id=None, saved: str = "") -> tuple[str, list]:
    players = _players(user_id)
    if not players:
        content = (
            "## 🎮 In-game notifications\n"
            "You haven't claimed a RuneScape account yet — use `/claim-rsn` "
            "first, then come back here."
        )
        return content, [ActionRow(_back_button())]

    if player_id is None and len(players) > 1:
        content = (
            f"{saved}## 🎮 In-game notifications\n"
            "Which account do you want to configure?"
        )
        select = StringSelectMenu(
            *[StringSelectOption(label=p["name"], value=str(p["id"]))
              for p in players[:25]],
            placeholder="Pick an account…",
            custom_id=f"{PREFIX}ig:acct",
        )
        return content, [ActionRow(select), ActionRow(_back_button())]

    target = next((p for p in players if p["id"] == player_id), players[0])
    types = _event_pref_types()
    state = _event_prefs_state(target["id"], types)
    content = (
        f"{saved}## 🎮 In-game notifications — `{target['name']}`\n"
        "Which event notifications the RuneLite plugin shows you in-game. "
        "Task-progress ticks are muted in the plugin's own settings "
        "(*Task progress notifications*)."
    )
    select = StringSelectMenu(
        *[
            StringSelectOption(label=EVENT_PREF_LABELS.get(t, t), value=t,
                               default=state[t])
            for t in types
        ],
        placeholder="Choose which in-game notifications you receive…",
        min_values=0,
        max_values=len(types),
        custom_id=f"{PREFIX}ig:set:{target['id']}",
    )
    return content, [ActionRow(select), ActionRow(_back_button())]


def build_pings_panel(user_id: int, saved: str = "") -> tuple[str, list]:
    state = _ping_state(user_id)
    content = (
        f"{saved}## 👁️ Pings & visibility\n"
        "Whether I @mention you when your submissions post to Discord, and "
        "whether your accounts appear in global listings. Unticking both ping "
        "options means you're never pinged.\n"
        "*(Hide a single account instead with `/hideme`.)*"
    )
    select = StringSelectMenu(
        StringSelectOption(
            label="Ping me in the global server", value="global",
            description="Mentions on your submissions in the DropTracker server",
            default=state["global_ping"] and not state["never_ping"]),
        StringSelectOption(
            label="Ping me in my clan's server(s)", value="group",
            description="Mentions on your submissions in clan servers",
            default=state["group_ping"] and not state["never_ping"]),
        StringSelectOption(
            label="Show my accounts in global listings", value="visible",
            description="Leaderboards, the global server, search",
            default=not state["hidden"]),
        placeholder="Choose your ping & visibility options…",
        min_values=0,
        max_values=3,
        custom_id=f"{PREFIX}pings:set",
    )
    return content, [ActionRow(select), ActionRow(_back_button())]


SAVED = "✅ **Saved.**\n"


class PlayerSettingsPanel(Extension):
    @slash_command(
        name="settings",
        description="Configure your DropTracker notifications & privacy without leaving Discord",
    )
    async def settings_cmd(self, ctx: SlashContext):
        user_id = _resolve_user_id(str(ctx.author.id))
        if user_id is None:
            await ctx.send(
                "I couldn't find a DropTracker account for you yet — claim an "
                "account with `/claim-rsn` (or sign in once at "
                f"{SETTINGS_URL}) and try again.",
                ephemeral=True,
            )
            return
        content, components = build_main_panel()
        await ctx.send(content, components=components, ephemeral=True)

    @listen(Component)
    async def on_component(self, event: Component):
        ctx: ComponentContext = event.ctx
        custom_id = ctx.custom_id or ""
        if not custom_id.startswith(PREFIX):
            return
        try:
            await self._route(ctx, custom_id[len(PREFIX):])
        except Exception as e:
            app_logger.log(log_type="error",
                           data=f"/settings panel failed on {custom_id}: {e}",
                           app_name="core", description="player_settings_panel")
            try:
                await ctx.send("Something went wrong — try `/settings` again.",
                               ephemeral=True)
            except Exception:
                pass

    async def _route(self, ctx: ComponentContext, action: str):
        user_id = _resolve_user_id(str(ctx.author.id))
        if user_id is None:
            await ctx.send(
                "I couldn't find a DropTracker account for you — use "
                "`/claim-rsn` first.", ephemeral=True)
            return

        if action == "home":
            content, components = build_main_panel()
        elif action == "dms":
            content, components = build_dms_panel(user_id)
        elif action == "dms:set":
            _set_config_values(user_id, dm_updates_from_selection(set(ctx.values)))
            content, components = build_dms_panel(user_id, saved=SAVED)
        elif action == "dms:minval":
            modal = Modal(
                ShortText(
                    label="Minimum drop value (GP) for drop DMs",
                    custom_id="value",
                    placeholder="e.g. 1000000 — 0 for every tracked drop",
                    required=True,
                ),
                title="Minimum drop value",
                custom_id=f"{PREFIX}minval",
            )
            await ctx.send_modal(modal)
            return
        elif action == "ig":
            content, components = build_ig_panel(user_id)
        elif action == "ig:acct":
            try:
                player_id = int(ctx.values[0])
            except (ValueError, IndexError):
                return
            content, components = build_ig_panel(user_id, player_id=player_id)
        elif action.startswith("ig:set:"):
            try:
                player_id = int(action.rsplit(":", 1)[1])
            except ValueError:
                return
            types = _event_pref_types()
            ok = _save_event_prefs(
                user_id, player_id,
                event_prefs_from_selection(set(ctx.values), types))
            content, components = build_ig_panel(
                user_id, player_id=player_id,
                saved=SAVED if ok else "⚠️ **That account isn't linked to you.**\n")
        elif action == "pings":
            content, components = build_pings_panel(user_id)
        elif action == "pings:set":
            _save_ping_state(user_id, set(ctx.values))
            content, components = build_pings_panel(user_id, saved=SAVED)
        else:
            return
        await ctx.edit_origin(content=content, components=components)

    @listen(ModalCompletion)
    async def on_modal(self, event: ModalCompletion):
        ctx = event.ctx
        if ctx.custom_id != f"{PREFIX}minval":
            return
        user_id = _resolve_user_id(str(ctx.author.id))
        if user_id is None:
            await ctx.send("I couldn't find your DropTracker account.",
                           ephemeral=True)
            return
        raw = (ctx.responses.get("value") or "").strip()
        cleaned = raw.replace(",", "").replace(" ", "").lower()
        # Accept shorthand ("5m", "250k") — it's how players write GP.
        multiplier = 1
        if cleaned.endswith("k"):
            multiplier, cleaned = 1_000, cleaned[:-1]
        elif cleaned.endswith("m"):
            multiplier, cleaned = 1_000_000, cleaned[:-1]
        elif cleaned.endswith("b"):
            multiplier, cleaned = 1_000_000_000, cleaned[:-1]
        try:
            value = int(float(cleaned) * multiplier)
        except (TypeError, ValueError):
            await ctx.send("That doesn't look like a GP amount — try e.g. "
                           "`1000000`, `1m` or `250k`.", ephemeral=True)
            return
        if value < 0 or value > DM_MIN_VALUE_MAX:
            await ctx.send("That amount is out of range.", ephemeral=True)
            return
        _set_config_values(user_id, {DM_MIN_VALUE_KEY: str(value)})
        suffix = "every tracked drop" if value == 0 else f"drops worth **{value:,} GP** or more"
        await ctx.send(f"✅ Drop DMs will now cover {suffix}.", ephemeral=True)


def setup(bot):
    PlayerSettingsPanel(bot)
