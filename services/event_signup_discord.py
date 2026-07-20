"""In-Discord event sign-up flow (the "Sign up" button).

An admin posts a sign-up prompt from the event manager
(``POST /events/{id}/signup-message``); the notification service attaches a
``Sign up`` button (custom_id ``evtsignup:{event_id}``). This extension drives
the button:

  evtsignup:{event_id}                 → ephemeral account picker (the user's
                                          linked RSNs — one entry per person).
  evtsignup_acct:{event_id}            → account chosen. self_join with >1 of
                                          the player's teams → team picker;
                                          otherwise finalize immediately.
  evtsignup_team:{event_id}:{player}   → team chosen → finalize.

All placement rules (mode, clan routing, one RSN per user) live in the shared
``services.event_signup.perform_signup`` so the button and the website behave
identically. The bot never trusts the client: every step re-loads the event and
re-checks ownership/eligibility.
"""
from __future__ import annotations

import logging

import interactions
from sqlalchemy.exc import IntegrityError
from interactions import (
    ActionRow,
    Extension,
    StringSelectMenu,
    StringSelectOption,
    listen,
)
from interactions.api.events import Component

from db.models import Event, EventTeam, Player, User, session
from services import event_signup as sus

log = logging.getLogger("event_signup_discord")

_PROMPT = "evtsignup:"
_ACCT = "evtsignup_acct:"
_TEAM = "evtsignup_team:"


def _linked_players(discord_id) -> list:
    """The user's linked OSRS accounts (id, name), or [] if not signed up."""
    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if not user:
        return []
    rows = (
        session.query(Player.player_id, Player.player_name)
        .filter(Player.user_id == user.user_id)
        .order_by(Player.player_name.asc())
        .all()
    )
    return [(pid, name) for pid, name in rows]


def _load_event(event_id: int):
    return session.query(Event).filter(Event.id == event_id).first()


async def _need_account_link(ctx) -> None:
    await ctx.send(
        "You don't have an OSRS account linked yet. Sign in at "
        "https://www.droptracker.io and link an account, then try again.",
        ephemeral=True,
    )


class EventSignupButtons(Extension):
    def __init__(self, bot):
        self.bot = bot

    @listen(Component)
    async def on_event_signup_component(self, event: Component):
        ctx = event.ctx
        custom_id = getattr(ctx, "custom_id", "") or ""
        try:
            if custom_id.startswith(_PROMPT):
                await self._start(ctx, int(custom_id[len(_PROMPT):]))
            elif custom_id.startswith(_ACCT):
                await self._pick_account(ctx, int(custom_id[len(_ACCT):]))
            elif custom_id.startswith(_TEAM):
                _, rest = custom_id.split(":", 1)
                event_id_s, player_id_s = rest.split(":", 1)
                await self._finalize(ctx, int(event_id_s), int(player_id_s))
        except sus.SignupError as exc:
            await self._reply(ctx, f"⚠️ {exc.detail}")
        except IntegrityError:
            # web59a unique backstop: a double click raced its twin and lost —
            # from the player's point of view the sign-up WORKED. Don't show
            # them "Something went wrong" for a success (audit).
            try:
                session.rollback()
            except Exception:
                pass
            await self._reply(
                ctx, "✅ You're already signed up — your team placement is set.")
        except Exception:
            log.exception("event signup component failed: %s", custom_id)
            try:
                await self._reply(ctx, "Something went wrong — please try again.")
            except Exception:
                pass

    # -- step 1: the Sign up button -> account picker ----------------------- #
    async def _start(self, ctx, event_id: int):
        await ctx.defer(ephemeral=True)
        session.expire_all()
        ev = _load_event(event_id)
        if not ev or not sus.is_self_signup_mode(ev):
            await self._reply(ctx, "Sign-ups for this event aren't open.")
            return
        players = _linked_players(ctx.user.id)
        if not players:
            await _need_account_link(ctx)
            return
        # A single account: skip the picker and go straight to team/finalize.
        if len(players) == 1:
            await self._route_after_account(ctx, ev, players[0][0])
            return
        options = [
            StringSelectOption(label=name[:100], value=str(pid))
            for pid, name in players[:25]
        ]
        await ctx.send(
            content=f"**Sign up for {ev.name}** — you can enter with **one** account. "
                    "Which one?",
            components=[ActionRow(StringSelectMenu(
                *options, placeholder="Choose your account…",
                custom_id=f"{_ACCT}{event_id}",
            ))],
            ephemeral=True,
        )

    # -- step 2: account chosen -> team picker or finalize ------------------ #
    async def _pick_account(self, ctx, event_id: int):
        values = list(getattr(ctx, "values", None) or [])
        if not values:
            return
        await ctx.defer(ephemeral=True, edit_origin=True)
        session.expire_all()
        ev = _load_event(event_id)
        if not ev:
            await self._reply(ctx, "That event no longer exists.")
            return
        await self._route_after_account(ctx, ev, int(values[0]))

    async def _route_after_account(self, ctx, ev, player_id: int):
        """self_join with a real choice → show teams; otherwise finalize."""
        player = self._owned_player(ctx, player_id)
        if player is None:
            await self._reply(ctx, "That account isn't linked to your Discord.")
            return
        formation = getattr(ev, "formation_mode", None) or "admin_assign"
        if formation == "self_join":
            teams = sus.eligible_teams(session, ev, player_id)
            if len(teams) > 1:
                options = [StringSelectOption(label=t.name[:100], value=str(t.id))
                           for t in teams[:25]]
                await ctx.send(
                    content=f"Which team should **{player.player_name}** join?",
                    components=[ActionRow(StringSelectMenu(
                        *options, placeholder="Choose a team…",
                        custom_id=f"{_TEAM}{ev.id}:{player_id}",
                    ))],
                    ephemeral=True,
                )
                return
        # auto_assign / signup_pool / self_join-with-one-team → finalize now.
        await self._commit_signup(ctx, ev, player, team_id=None)

    # -- step 3: team chosen -> finalize ------------------------------------ #
    async def _finalize(self, ctx, event_id: int, player_id: int):
        values = list(getattr(ctx, "values", None) or [])
        if not values:
            return
        await ctx.defer(ephemeral=True, edit_origin=True)
        session.expire_all()
        ev = _load_event(event_id)
        if not ev:
            await self._reply(ctx, "That event no longer exists.")
            return
        player = self._owned_player(ctx, player_id)
        if player is None:
            await self._reply(ctx, "That account isn't linked to your Discord.")
            return
        await self._commit_signup(ctx, ev, player, team_id=int(values[0]))

    async def _commit_signup(self, ctx, ev, player, team_id):
        try:
            result = sus.perform_signup(
                session, ev, player, self._user_id(ctx),
                team_id=team_id, source="discord",
            )
            session.commit()
        except sus.SignupError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        self._bump(ev.id)
        if result.get("pooled"):
            msg = (f"✅ **{player.player_name}** is in the sign-up pool for "
                   f"**{ev.name}**. Team assignments come later — watch this channel!")
        else:
            team = session.query(EventTeam).filter(EventTeam.id == result["team_id"]).first()
            tname = team.name if team else "your team"
            msg = f"✅ **{player.player_name}** joined **{tname}** for **{ev.name}**. Good luck!"
        await self._reply(ctx, msg)

    # -- helpers ------------------------------------------------------------ #
    def _owned_player(self, ctx, player_id: int):
        user = session.query(User).filter(User.discord_id == str(ctx.user.id)).first()
        if not user:
            return None
        return (
            session.query(Player)
            .filter(Player.player_id == player_id, Player.user_id == user.user_id)
            .first()
        )

    def _user_id(self, ctx) -> int:
        user = session.query(User).filter(User.discord_id == str(ctx.user.id)).first()
        return user.user_id if user else None

    def _bump(self, event_id: int) -> None:
        try:
            from services.event_engine import publish_event_admin_bump

            publish_event_admin_bump(event_id)
        except Exception:
            pass

    async def _reply(self, ctx, message: str) -> None:
        try:
            await ctx.send(message, ephemeral=True)
        except Exception:
            log.debug("event signup: could not reply", exc_info=True)
