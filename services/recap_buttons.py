"""Buttons on the monthly recap messages.

Three on the player's DM. Everyone receives one recap unsolicited, and these
are how they say whether they want the next one — and which of their accounts
it should cover. Doing it in the message matters: the alternative is "go to
the website and find a checkbox", which most people will not do, so the opt-in
rate would measure friction rather than interest.

* **Keep sending these** / **No thanks** (or **Stop sending these** on a card
  they asked for) — the opt-in itself.
* **Choose accounts** — opens a private picker listing every account they have
  linked, with the current choice ticked: the biggest month (the default),
  every account, or the ones they name. Saving a *changed* choice also opts
  them in (choosing accounts is asking for the mail) and sends a message into
  the DM stating what will be sent from now on, so the decision is on record
  under the card it was made from. Saving the same choice again says so and
  sends nothing. Ticking nothing is "no accounts", which is the opt-out.

One on the clan's channel post, which opens that month's loot leaderboard
privately. A clan card shows the top five; the button answers "where did *I*
come?" without another message in the channel, and it reads the same stored
snapshot the card was drawn from, so the numbers can never disagree with the
picture above them.

Every handler is persistent: it matches on ``custom_id`` and carries its subject
in that id rather than holding state about a specific message, so a button
pressed months after the card was posted still works, across bot restarts. Same
approach as :mod:`services.news_optin` — with one difference that matters. That
one runs in a guild and toggles a role; the buttons here run in a **DM**,
where ``ctx.guild`` is None and there is no Member. The presser is identified by
their Discord id and resolved to a DropTracker user, which is also what makes
them safe to press from anywhere: they only ever edit the row belonging to
whoever pressed them.

The pure half of the account picker — what the options are, what a selection
means, how the result is worded — lives in :mod:`services.recap_delivery`
beside the code that reads the setting, so the two can never disagree about
what a stored value means; this module only turns it into components.
"""
from __future__ import annotations

from typing import Optional

from interactions import (
    ActionRow,
    ComponentContext,
    Extension,
    StringSelectMenu,
    StringSelectOption,
    listen,
)
from interactions.api.events import Component

from db.app_logger import AppLogger

app_logger = AppLogger()

OPT_IN_ID = "recap_optin:on"
OPT_OUT_ID = "recap_optin:off"
# `recap_lb:{group_id}:{period}` — the subject travels in the id so the handler
# needs no memory of which message it came from.
LEADERBOARD_PREFIX = "recap_lb:"
# How many places the private leaderboard shows. The snapshot stores ten.
LEADERBOARD_ROWS = 10
# `recap_accounts:pick:{player_id}` opens the picker (the card's own account
# rides along so the picker can say what the default meant this month);
# `recap_accounts:set` is the select inside it.
ACCOUNT_PICK_PREFIX = "recap_accounts:pick:"
ACCOUNT_SET_ID = "recap_accounts:set"

# Mirrors services/recap_delivery.USER_CFG_OPT_IN / USER_CFG_ACCOUNTS and
# web_api/routes/me.py. The ids above mirror recap_delivery's too; that module
# cannot import this one (it would drag the Discord client into the delivery
# script), so tests/unit/test_recap_buttons.py pins the copies equal.
CONFIG_KEY = "dm_monthly_recap"
ACCOUNTS_KEY = "recap_accounts"

SETTINGS_URL = "https://www.droptracker.io/settings"
_UNLINKED = (
    "Couldn't find a DropTracker account linked to this Discord user — "
    f"sign in at {SETTINGS_URL} and you can set this there."
)


# --------------------------------------------------------------------------- #
# Storage (one short-lived session per interaction)
# --------------------------------------------------------------------------- #
def _account_state(
    discord_id: str,
) -> Optional[tuple[int, list[tuple[int, str, bool]], str, bool]]:
    """``(user_id, players, recap_accounts, opted_in)`` for whoever pressed.

    ``players`` is every account they have linked, as ``(id, name, hidden)``,
    by name. None when the Discord account has never linked one — worth saying
    out loud rather than silently pretending the click worked.
    """
    from sqlalchemy import text

    from db.models import Session as _Session

    s = _Session()
    try:
        row = s.execute(
            text("SELECT user_id FROM users WHERE discord_id = :did LIMIT 1"),
            {"did": str(discord_id)},
        ).first()
        if not row:
            return None
        user_id = int(row[0])
        players = [
            (int(pid), (name or f"Player {pid}"), bool(hidden))
            for pid, name, hidden in s.execute(
                text(
                    "SELECT player_id, player_name, COALESCE(hidden, 0) FROM players "
                    "WHERE user_id = :uid ORDER BY player_name ASC"
                ),
                {"uid": user_id},
            ).fetchall()
        ]
        cfg = {
            key: (value or "")
            for key, value in s.execute(
                text(
                    "SELECT config_key, config_value FROM user_configurations "
                    "WHERE user_id = :uid AND config_key IN (:opt, :acct)"
                ),
                {"uid": user_id, "opt": CONFIG_KEY, "acct": ACCOUNTS_KEY},
            ).fetchall()
        }
        opted_in = str(cfg.get(CONFIG_KEY, "")).strip().lower() in ("1", "true", "yes", "on")
        return user_id, players, str(cfg.get(ACCOUNTS_KEY, "")).strip(), opted_in
    finally:
        s.close()


def _write_config(user_id: int, updates: dict[str, str]) -> bool:
    """Upsert ``user_configurations`` rows for one user. False on failure,
    which the caller says out loud instead of confirming a save that didn't
    happen."""
    from sqlalchemy import text

    from db.models import Session as _Session

    s = _Session()
    try:
        for key, value in updates.items():
            s.execute(
                text(
                    "INSERT INTO user_configurations (user_id, config_key, config_value) "
                    "VALUES (:uid, :key, :val) "
                    "ON DUPLICATE KEY UPDATE config_value = :val"
                ),
                {"uid": int(user_id), "key": key, "val": value},
            )
        s.commit()
        return True
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"recap preference write failed for user {user_id} ({sorted(updates)}): {e}",
            app_name="core",
            description="recap_buttons",
        )
        return False
    finally:
        s.close()


def _group_name(group_id: int) -> str:
    from db.models import Session as _Session
    from sqlalchemy import text

    s = _Session()
    try:
        row = s.execute(
            text("SELECT group_name FROM groups WHERE group_id = :gid"),
            {"gid": group_id},
        ).first()
        return (row[0] if row else None) or f"Group {group_id}"
    except Exception:
        return f"Group {group_id}"
    finally:
        s.close()


async def _leaderboard_embed(group_id: int, period: str) -> Optional[dict]:
    """That month's lootboard — the graphical board, not a rewritten list.

    It's the same image the clan already sees in its own channel, rendered for
    the month the card covers. Frozen at delivery time, so this is normally a
    file that already exists; generating here is the fallback for a board that
    was pruned or a message older than the file.
    """
    from services.recap_delivery import ensure_group_lootboard, format_period, group_recap_url

    try:
        url = await ensure_group_lootboard(group_id, period)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"recap lootboard failed for group {group_id} {period}: {e}",
            app_name="core",
            description="recap_buttons",
        )
        return None
    if not url:
        return None

    return {
        "title": f"{_group_name(group_id)} — {format_period(period)} loot",
        "url": group_recap_url(group_id, period),
        "image": {"url": url},
        "color": 0xC8A24C,
        "footer": {"text": "Only you can see this · full recap on the site"},
    }


# --------------------------------------------------------------------------- #
# The account picker
# --------------------------------------------------------------------------- #
def build_picker(
    players: list[tuple[int, str, bool]],
    preference: str,
    opted_in: bool,
    *,
    note: str = "",
    card_player_id: Optional[int] = None,
) -> tuple[str, list]:
    """The ephemeral picker: one multi-select with the current choice ticked.

    ``note`` is a line for the top ("no change", "couldn't read that") when the
    picker is re-shown after a submission. ``card_player_id`` is the account of
    the card the button was pressed on — with the default in force, that is
    what "the biggest month" resolved to, which is more useful to see than the
    rule alone.
    """
    from services.recap_delivery import (
        MODE_BEST,
        account_picker_options,
        parse_account_preference,
        preference_phrase,
    )

    if opted_in:
        current = f"Currently: {preference_phrase(preference, players)}."
        mode, _ids = parse_account_preference(preference)
        if mode == MODE_BEST and card_player_id is not None:
            name = next((n for pid, n, _h in players if pid == card_player_id), None)
            if name:
                current += f" This month that was **{name}**."
    else:
        current = (
            "Currently: off — this was your one free recap. Pick accounts below "
            "and we'll keep them coming."
        )
    content = (
        f"{note}**Which accounts should get a monthly recap?**\n"
        f"{current}\n"
        "Tick the accounts you want a card for, or one of the two automatic "
        "options. Tick nothing to stop these DMs."
    )
    options = []
    for option in account_picker_options(players, preference):
        extra = {"description": option["description"]} if option.get("description") else {}
        options.append(
            StringSelectOption(
                label=option["label"], value=option["value"],
                default=bool(option["default"]), **extra,
            )
        )
    select = StringSelectMenu(
        *options,
        placeholder="Choose which accounts get a recap…",
        min_values=0,
        max_values=len(options),
        custom_id=ACCOUNT_SET_ID,
    )
    return content, [ActionRow(select)]


async def _send_dm(ctx: ComponentContext, content: str) -> bool:
    """A plain message into the DM — the record of the choice. False when it
    couldn't be sent (closed DMs since the card arrived), so the caller can
    put the same words in the ephemeral reply instead."""
    try:
        await ctx.author.send(content)
        return True
    except Exception as e:
        app_logger.log(
            log_type="warning",
            data=f"recap preference confirmation DM failed for {ctx.author.id}: {e}",
            app_name="core",
            description="recap_buttons",
        )
        return False


class RecapButtons(Extension):
    @listen(Component)
    async def on_component(self, event: Component):
        ctx = event.ctx
        custom_id = ctx.custom_id or ""
        try:
            if custom_id == OPT_IN_ID:
                await self._respond(ctx, True)
            elif custom_id == OPT_OUT_ID:
                await self._respond(ctx, False)
            elif custom_id.startswith(LEADERBOARD_PREFIX):
                await self._leaderboard(ctx, custom_id)
            elif custom_id.startswith(ACCOUNT_PICK_PREFIX):
                await self._open_picker(ctx, custom_id)
            elif custom_id == ACCOUNT_SET_ID:
                await self._apply_selection(ctx)
        except Exception as e:
            app_logger.log(
                log_type="error",
                data=f"recap button {custom_id} failed for {ctx.author.id}: {e}",
                app_name="core",
                description="recap_buttons",
            )
            try:
                await ctx.send("Something went wrong — try again in a moment.", ephemeral=True)
            except Exception:
                pass

    async def _leaderboard(self, ctx: ComponentContext, custom_id: str):
        try:
            _, raw_group, period = custom_id.split(":", 2)
            group_id = int(raw_group)
        except (ValueError, TypeError):
            return
        # Deferred first: the board is normally already on disk, but the
        # fallback render takes a second or two — longer than Discord's 3s
        # window for a first response.
        await ctx.defer(ephemeral=True)
        embed = await _leaderboard_embed(group_id, period)
        if not embed:
            await ctx.send(
                "That month's lootboard isn't available any more.", ephemeral=True
            )
            return
        # Ephemeral: dozens of people pressing this shouldn't each add a message
        # to the channel the card was posted in.
        await ctx.send(embeds=[embed], ephemeral=True)

    async def _respond(self, ctx: ComponentContext, opted_in: bool):
        from services.recap_delivery import CHOOSE_ACCOUNTS_LABEL, preference_phrase

        state = _account_state(str(ctx.author.id))
        if state is None or not _write_config(state[0], {CONFIG_KEY: "true" if opted_in else "false"}):
            await ctx.send(_UNLINKED, ephemeral=True)
            return
        _user_id, players, preference, _was_opted_in = state
        if opted_in:
            message = "✅ You'll get your recap on the 1st of each month"
            if len(players) > 1:
                # Say what the default means for them, and where to change it —
                # opting in is the moment the account question first matters.
                message += (
                    f" — for {preference_phrase(preference, players)}. "
                    f"**{CHOOSE_ACCOUNTS_LABEL}** below picks which."
                )
            else:
                message += "."
            message += f" Change it any time in your [settings]({SETTINGS_URL})."
        else:
            message = (
                "🔕 No more monthly recaps. You can still read them any time on your "
                "profile, and turn them back on in your "
                f"[settings]({SETTINGS_URL})."
            )
        await ctx.send(message, ephemeral=True)

    async def _open_picker(self, ctx: ComponentContext, custom_id: str):
        try:
            card_player_id: Optional[int] = int(custom_id[len(ACCOUNT_PICK_PREFIX):])
        except ValueError:
            card_player_id = None
        state = _account_state(str(ctx.author.id))
        if state is None:
            await ctx.send(_UNLINKED, ephemeral=True)
            return
        _user_id, players, preference, opted_in = state
        if not players:
            await ctx.send(
                "You don't have any accounts linked yet — claim one with "
                "`/claim-rsn` and the next recap can cover it.",
                ephemeral=True,
            )
            return
        content, components = build_picker(
            players, preference, opted_in, card_player_id=card_player_id
        )
        # Ephemeral even in a DM: the picker is a control, not a message worth
        # keeping, and closing it on save leaves the record (below) alone.
        await ctx.send(content, components=components, ephemeral=True)

    async def _apply_selection(self, ctx: ComponentContext):
        from services.recap_delivery import (
            format_account_preference,
            parse_account_preference,
            preference_from_selection,
            preference_phrase,
            preference_summary,
        )

        # Edit the picker in place. Deferred first: two reads, a write and a DM
        # can outrun Discord's 3s first-response window on a busy box, and a
        # timed-out interaction shows "This interaction failed" over a save
        # that actually happened.
        await ctx.defer(edit_origin=True)
        state = _account_state(str(ctx.author.id))
        if state is None:
            await ctx.edit_origin(content=_UNLINKED, components=[])
            return
        user_id, players, current, opted_in = state

        try:
            chosen = preference_from_selection(
                list(getattr(ctx, "values", None) or []), [pid for pid, _n, _h in players]
            )
        except ValueError:
            content, components = build_picker(
                players, current, opted_in,
                note="⚠️ **That selection didn't name any of your accounts** — try again.\n",
            )
            await ctx.edit_origin(content=content, components=components)
            return

        if chosen is None:
            # Nothing ticked: "no accounts" is the opt-out. Their account list
            # is kept, so opting back in later restores the same choice.
            changed = opted_in
            updates = {CONFIG_KEY: "false"}
            next_preference, next_opted_in = current, False
        else:
            # Compare canonical forms: "3,1" and "1,3" are the same choice.
            same_accounts = format_account_preference(*parse_account_preference(current)) == chosen
            changed = (not opted_in) or not same_accounts
            updates = {CONFIG_KEY: "true", ACCOUNTS_KEY: chosen}
            next_preference, next_opted_in = chosen, True

        if not changed:
            note = "ℹ️ **No change** — " + (
                f"recaps still cover {preference_phrase(current, players)}.\n"
                if opted_in
                else "recap DMs are already off.\n"
            )
            content, components = build_picker(players, current, opted_in, note=note)
            await ctx.edit_origin(content=content, components=components)
            return

        if not _write_config(user_id, updates):
            await ctx.edit_origin(
                content="Couldn't save that — try again in a moment.", components=[]
            )
            return

        summary = preference_summary(next_preference, players, opted_in=next_opted_in)
        # The record goes into the DM proper, under the card the choice was
        # made from; the ephemeral picker just closes.
        delivered = await _send_dm(ctx, summary)
        await ctx.edit_origin(
            content=(
                "✅ **Saved** — the details are in the message below."
                if delivered
                else summary
            ),
            components=[],
        )


def setup(bot):
    RecapButtons(bot)
