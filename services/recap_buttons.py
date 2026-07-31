"""Buttons on the monthly recap messages.

Two on the player's DM — everyone receives one recap unsolicited, and these are
how they say whether they want the next one. Doing it in the message matters:
the alternative is "go to the website and find a checkbox", which most people
will not do, so the opt-in rate would measure friction rather than interest.

One on the clan's channel post, which opens that month's loot leaderboard
privately. A clan card shows the top five; the button answers "where did *I*
come?" without another message in the channel, and it reads the same stored
snapshot the card was drawn from, so the numbers can never disagree with the
picture above them.

Every handler is persistent: it matches on ``custom_id`` and carries its subject
in that id rather than holding state about a specific message, so a button
pressed months after the card was posted still works, across bot restarts. Same
approach as :mod:`services.news_optin` — with one difference that matters. That
one runs in a guild and toggles a role; the opt-in buttons here run in a **DM**,
where ``ctx.guild`` is None and there is no Member. The presser is identified by
their Discord id and resolved to a DropTracker user, which is also what makes
them safe to press from anywhere: they only ever edit the row belonging to
whoever pressed them.
"""
from __future__ import annotations

from typing import Optional

from interactions import ComponentContext, Extension, listen
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

# Mirrors services/recap_delivery.USER_CFG_OPT_IN and web_api/routes/me.py.
CONFIG_KEY = "dm_monthly_recap"


def _set_opt_in(discord_id: str, value: bool) -> bool:
    """Write the preference for the user behind ``discord_id``.

    Returns False when we can't identify them — which happens for a Discord
    account that has never linked one, and is worth saying out loud rather than
    silently pretending the click worked.
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
            return False
        s.execute(
            text(
                "INSERT INTO user_configurations (user_id, config_key, config_value) "
                "VALUES (:uid, :key, :val) "
                "ON DUPLICATE KEY UPDATE config_value = :val"
            ),
            {"uid": int(row[0]), "key": CONFIG_KEY, "val": "true" if value else "false"},
        )
        s.commit()
        return True
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"recap opt-in write failed for discord {discord_id}: {e}",
            app_name="core",
            description="recap_optin",
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


class RecapButtons(Extension):
    @listen(Component)
    async def on_component(self, event: Component):
        custom_id = event.ctx.custom_id
        if custom_id == OPT_IN_ID:
            await self._respond(event.ctx, True)
        elif custom_id == OPT_OUT_ID:
            await self._respond(event.ctx, False)
        elif custom_id.startswith(LEADERBOARD_PREFIX):
            await self._leaderboard(event.ctx, custom_id)

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
        ok = _set_opt_in(str(ctx.author.id), opted_in)
        if not ok:
            await ctx.send(
                "Couldn't find a DropTracker account linked to this Discord user — "
                "sign in at https://www.droptracker.io/settings and you can set this there.",
                ephemeral=True,
            )
            return
        if opted_in:
            message = (
                "✅ You'll get your recap on the 1st of each month. "
                "Change it any time in your [settings](https://www.droptracker.io/settings)."
            )
        else:
            message = (
                "🔕 No more monthly recaps. You can still read them any time on your "
                "profile, and turn them back on in your "
                "[settings](https://www.droptracker.io/settings)."
            )
        await ctx.send(message, ephemeral=True)


def setup(bot):
    RecapButtons(bot)
