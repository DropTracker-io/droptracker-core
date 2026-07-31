"""The two buttons on a monthly recap DM.

Everyone receives one recap unsolicited; these buttons are how they say whether
they want the next one. Doing it in the message matters — the alternative is
"go to the website and find a checkbox", which most people will not do, so the
opt-in rate would measure friction rather than interest.

The handler is persistent: it matches on ``custom_id`` rather than holding state
about a specific message, so a button pressed months after the card was sent
still works, across bot restarts. Same approach as
:mod:`services.news_optin` — with one difference that matters. That one runs in
a guild and toggles a role; this one runs in a **DM**, where ``ctx.guild`` is
None and there is no Member. The presser is identified by their Discord id and
resolved to a DropTracker user, which is also what makes the button safe to
press from anywhere: it only ever edits the row belonging to whoever pressed it.
"""
from __future__ import annotations

from interactions import ComponentContext, Extension, listen
from interactions.api.events import Component

from db.app_logger import AppLogger

app_logger = AppLogger()

OPT_IN_ID = "recap_optin:on"
OPT_OUT_ID = "recap_optin:off"

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


class RecapOptIn(Extension):
    @listen(Component)
    async def on_component(self, event: Component):
        custom_id = event.ctx.custom_id
        if custom_id == OPT_IN_ID:
            await self._respond(event.ctx, True)
        elif custom_id == OPT_OUT_ID:
            await self._respond(event.ctx, False)

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
    RecapOptIn(bot)
