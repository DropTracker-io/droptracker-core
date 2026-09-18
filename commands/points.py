"""
Points Commands Module

The clan-points commands every member can use:

    /group-points [period]   the clan's board -- paged, with a period picker,
                             a "my rank" jump and a link to the full board
    /my-points               your own standing, accounts and recent awards
    /lookup [player|member]  the same card for somebody else

Who stands where -- current members only, RSNs optionally combined per Discord
user -- is decided in ``db/point_standings.py``, the same module the website
board and the notification totals read, so the three can never disagree. The
wording, paging arithmetic and button ids live in ``services/points_cards.py``
(pure, unit-tested); this module is the Discord half.

Two rules this module keeps:

* **Every read runs off the event loop, on its own Session.** A board is a
  handful of aggregate queries; on the scoped session they would block the
  gateway heartbeat for their duration and share state with every other
  coroutine in the bot. ``asyncio.to_thread`` + ``db_session()`` does neither,
  and the workers hand back plain data, never ORM objects.
* **A card never tells a viewer more than the clan already shows them.**
  Looking up a Discord member lists only their accounts *in this server's
  clan*; an RSN lookup never names the Discord user behind it; and an account
  hidden with /hideme is only ever shown to its owner.

Classes:
    PointsCommands: Extension containing the member-facing points commands
"""
from __future__ import annotations

import asyncio
from typing import Optional

from interactions import (
    ActionRow,
    AutocompleteContext,
    Button,
    ButtonStyle,
    ComponentContext,
    Embed,
    Extension,
    OptionType,
    SlashCommandChoice,
    SlashContext,
    StringSelectMenu,
    StringSelectOption,
    listen,
    slash_command,
    slash_option,
)
from interactions.api.events import Component
from sqlalchemy import text

from db.app_logger import AppLogger
from services import points_cards as cards

app_logger = AppLogger()

EMBED_COLOR = 0xD4A017


# --------------------------------------------------------------------------- #
# Reads (sync; always called through asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _group_for_guild(s, guild_id) -> Optional[dict]:
    if not guild_id:
        return None
    row = s.execute(
        text(
            "SELECT g.group_id, g.group_name FROM guilds gu "
            "JOIN `groups` g ON g.group_id = gu.group_id "
            "WHERE gu.guild_id = :guild LIMIT 1"
        ),
        {"guild": str(guild_id)},
    ).first()
    if row is None:
        return None
    return {"group_id": int(row[0]), "group_name": row[1] or f"Group #{row[0]}"}


def _accounts_where(s, clause: str, params: dict) -> list:
    rows = s.execute(
        text(
            "SELECT p.player_id, p.player_name, p.user_id, p.total_level, p.log_slots, "
            "COALESCE(p.hidden, 0), COALESCE(u.hidden, 0) "
            "FROM players p LEFT JOIN users u ON u.user_id = p.user_id "
            f"WHERE {clause} ORDER BY p.player_id"
        ),
        params,
    ).fetchall()
    return [
        {
            "player_id": int(pid),
            "name": name or f"Player {pid}",
            "user_id": int(uid) if uid is not None else None,
            "total_level": level,
            "log_slots": slots,
            "hidden": bool(p_hidden) or bool(u_hidden),
        }
        for pid, name, uid, level, slots, p_hidden, u_hidden in rows
    ]


def _accounts_of_discord_user(s, discord_id) -> tuple:
    """``(user_id | None, accounts)`` for a Discord id. ``user_id`` 0 and
    negative ids are real users, so absence is None and nothing else."""
    row = s.execute(
        text("SELECT user_id FROM users WHERE discord_id = :did LIMIT 1"),
        {"did": str(discord_id)},
    ).first()
    if row is None:
        return None, []
    user_id = int(row[0])
    return user_id, _accounts_where(s, "p.user_id = :uid", {"uid": user_id})


def _add_month_loot(accounts: list) -> list:
    """Best-effort Redis read; a card is still a card without it."""
    try:
        from services.redis_updates import get_player_current_month_total

        for account in accounts:
            account["month_loot"] = int(get_player_current_month_total(account["player_id"]) or 0)
    except Exception:
        pass
    return accounts


def _points_active(group_id: int) -> bool:
    try:
        from db.entitlements import group_has_entitlement

        return bool(group_has_entitlement(group_id, "custom_points"))
    except Exception:
        return True


def _load_board(guild_id, discord_id, period: str, page, *, jump_to_viewer: bool = False) -> dict:
    """The board spec for one page, or ``{"error": ...}``."""
    from db.models.base import db_session
    from db.point_standings import (
        combine_enabled,
        load_seasons,
        load_standings,
        resolve_period,
    )

    with db_session() as s:
        group = _group_for_guild(s, guild_id)
        if group is None:
            return {"error": "This server is not linked to a DropTracker group."}
        gid = group["group_id"]
        seasons = load_seasons(s, gid)

        sid = cards.season_id_of(period)
        if sid is not None:
            season = next((x for x in seasons if x["id"] == sid), None)
            if season is None:
                return {"error": "That season no longer exists — pick another period."}
            token, start, end = f"season:{sid}", season["start_at"], season["end_at"]
        else:
            try:
                token, start, end = resolve_period(period)
            except ValueError as exc:
                return {"error": str(exc)}

        combine = combine_enabled(s, gid)
        standings = load_standings(s, gid, start=start, end=end, combine=combine)
        _user_id, accounts = _accounts_of_discord_user(s, discord_id)

    viewer_ids = [a["player_id"] for a in accounts]
    view = cards.board_view(
        group_id=gid, group_name=group["group_name"], standings=standings,
        period=token, seasons=seasons, page=page if isinstance(page, int) else 1,
        combined=combine, viewer_player_ids=viewer_ids,
    )
    if jump_to_viewer and view["viewer_page"] and view["viewer_page"] != view["page"]:
        view = cards.board_view(
            group_id=gid, group_name=group["group_name"], standings=standings,
            period=token, seasons=seasons, page=view["viewer_page"],
            combined=combine, viewer_player_ids=viewer_ids,
        )
    view["group_id"] = gid
    return view


def _player_model():
    """The ORM Player, behind a seam: ``find_player_by_rsn`` is the one rule for
    matching a typed RSN and it takes a model, so the unit suite swaps in a
    SQLite-mapped stand-in here instead of this module growing a second copy of
    the rule in raw SQL."""
    from db.models import Player

    return Player


def _load_card(guild_id, viewer_discord_id, *, target_discord_id=None, rsn: Optional[str] = None) -> dict:
    """The player-card spec for a self view, a member lookup or an RSN lookup."""
    from db.models.base import db_session
    from db.point_standings import group_names, load_points_card, member_player_ids, totals_by_group
    from utils.rsn import find_player_by_rsn

    with db_session() as s:
        group = _group_for_guild(s, guild_id)
        gid = group["group_id"] if group else None
        _viewer_user_id, viewer_accounts = _accounts_of_discord_user(s, viewer_discord_id)
        viewer_ids = {a["player_id"] for a in viewer_accounts}

        if rsn:
            found = find_player_by_rsn(s, _player_model(), rsn)
            if found is None:
                return {"error": f"No player named `{rsn}` is tracked by DropTracker."}
            accounts = _accounts_where(s, "p.player_id = :pid", {"pid": int(found.player_id)})
            is_self = bool(accounts) and accounts[0]["player_id"] in viewer_ids
        elif target_discord_id is not None and str(target_discord_id) != str(viewer_discord_id):
            if gid is None:
                return {"error": "Looking up a Discord member only works inside a clan's server."}
            _target_user, accounts = _accounts_of_discord_user(s, target_discord_id)
            # Only what this clan can already see: their accounts in THIS clan.
            in_clan = member_player_ids(s, gid, [a["player_id"] for a in accounts])
            accounts = [a for a in accounts if a["player_id"] in in_clan]
            is_self = False
            if not accounts:
                return {"error": f"That member has no accounts in **{group['group_name']}**."}
        else:
            accounts, is_self = viewer_accounts, True
            if not accounts:
                return {"error": "You haven't claimed an account yet — use `/claim-rsn` first."}

        if not is_self:
            accounts = [a for a in accounts if not a["hidden"]]
            if not accounts:
                return {"error": "That player has chosen to hide their profile."}

        ids = [a["player_id"] for a in accounts]
        card = load_points_card(s, gid, ids) if gid is not None else None
        other_groups = []
        if is_self:
            totals = totals_by_group(s, ids)
            totals.pop(gid, None)
            names = group_names(s, list(totals))
            other_groups = sorted(
                ((names.get(g, f"Group #{g}"), pts) for g, pts in totals.items()),
                key=lambda item: -item[1],
            )

    return cards.player_card(
        accounts=_add_month_loot(accounts), is_self=is_self, group_id=gid,
        group_name=group["group_name"] if group else None, card=card,
        other_groups=other_groups,
        points_active=_points_active(gid) if gid is not None else True,
    )


def _suggest_names(guild_id, typed: str) -> list:
    from db.models.base import db_session
    from db.point_standings import search_member_names

    with db_session() as s:
        group = _group_for_guild(s, guild_id)
        return search_member_names(s, group["group_id"] if group else None, typed)


# --------------------------------------------------------------------------- #
# Specs -> Discord objects
# --------------------------------------------------------------------------- #
def _board_message(view: dict) -> tuple:
    embed = Embed(title=view["title"], description=view["description"], color=EMBED_COLOR)
    embed.set_footer(text=view["footer"])
    buttons = ActionRow(
        Button(style=ButtonStyle.SECONDARY, label="◀ Prev",
               custom_id=view["prev_id"], disabled=not view["can_prev"]),
        Button(style=ButtonStyle.SECONDARY, label="Next ▶",
               custom_id=view["next_id"], disabled=not view["can_next"]),
        Button(style=ButtonStyle.PRIMARY, label="My rank",
               custom_id=view["me_id"], disabled=not view["can_me"]),
        Button(style=ButtonStyle.URL, label="Full leaderboard", url=view["url"]),
    )
    picker = ActionRow(
        StringSelectMenu(
            *[
                StringSelectOption(label=o["label"], value=o["value"], default=o["default"])
                for o in view["period_options"]
            ],
            custom_id=view["period_select_id"],
            placeholder="Choose a period",
            min_values=1,
            max_values=1,
        )
    )
    return embed, [buttons, picker]


def _card_embed(spec: dict) -> Embed:
    embed = Embed(title=spec["title"], description=spec["description"] or None,
                  color=EMBED_COLOR, url=spec.get("url"))
    for name, value, inline in spec["fields"]:
        embed.add_field(name=name, value=value[:1024], inline=inline)
    embed.set_footer(text=spec["footer"])
    return embed


# --------------------------------------------------------------------------- #
# Extension
# --------------------------------------------------------------------------- #
class PointsCommands(Extension):
    """Member-facing clan points commands, and the buttons on their replies."""

    def __init__(self, bot):
        self.bot = bot

    # -- /group-points ------------------------------------------------------ #
    @slash_command(
        name="group-points",
        description="View this clan's points leaderboard",
    )
    @slash_option(
        name="period",
        description="Which standings to show (default: all-time). Seasons are in the reply's picker.",
        required=False,
        opt_type=OptionType.STRING,
        choices=[SlashCommandChoice(name=label, value=value) for value, label in cards.PRESETS],
    )
    async def group_points_cmd(self, ctx: SlashContext, period: str = "all"):
        if not ctx.guild_id:
            return await ctx.send("Use this command inside your clan's Discord server.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        view = await asyncio.to_thread(_load_board, ctx.guild_id, ctx.author.id, period, 1)
        if view.get("error"):
            return await ctx.send(view["error"], ephemeral=True)
        embed, components = _board_message(view)
        await ctx.send(embeds=[embed], components=components, ephemeral=True)

    # -- /my-points --------------------------------------------------------- #
    @slash_command(
        name="my-points",
        description="View your clan points: your rank, your accounts and your recent awards",
    )
    async def my_points_cmd(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        spec = await asyncio.to_thread(_load_card, ctx.guild_id, ctx.author.id)
        if spec.get("error"):
            return await ctx.send(spec["error"], ephemeral=True)
        await ctx.send(embeds=[_card_embed(spec)], ephemeral=True)

    # -- /lookup ------------------------------------------------------------ #
    @slash_command(
        name="lookup",
        description="Look up a player's clan points and DropTracker stats",
    )
    @slash_option(
        name="player",
        description="An in-game name (RSN)",
        required=False,
        opt_type=OptionType.STRING,
        autocomplete=True,
    )
    @slash_option(
        name="member",
        description="Or a member of this Discord server",
        required=False,
        opt_type=OptionType.USER,
    )
    async def lookup_cmd(self, ctx: SlashContext, player: str = None, member=None):
        if player and member is not None:
            return await ctx.send("Pick a player **or** a member, not both.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        spec = await asyncio.to_thread(
            _load_card, ctx.guild_id, ctx.author.id,
            target_discord_id=member.id if member is not None else None,
            rsn=(player or "").strip() or None,
        )
        if spec.get("error"):
            return await ctx.send(spec["error"], ephemeral=True)
        await ctx.send(embeds=[_card_embed(spec)], ephemeral=True)

    @lookup_cmd.autocomplete("player")
    async def lookup_autocomplete(self, ctx: AutocompleteContext):
        try:
            names = await asyncio.to_thread(_suggest_names, ctx.guild_id, ctx.input_text or "")
        except Exception:
            names = []
        await ctx.send(choices=[{"name": n, "value": n} for n in names[:25]])

    # -- buttons on the board ------------------------------------------------ #
    @listen(Component)
    async def on_component(self, event: Component):
        ctx: ComponentContext = event.ctx
        custom_id = ctx.custom_id or ""
        if not custom_id.startswith(
            (cards.NAV_PREFIX, cards.ME_PREFIX, cards.PERIOD_SELECT_PREFIX)
        ):
            return
        try:
            await self._turn_page(ctx, custom_id)
        except Exception as e:
            app_logger.log(
                log_type="error",
                data=f"points board button {custom_id} failed for {ctx.author.id}: {e}",
                app_name="core",
                description="points_commands",
            )
            try:
                await ctx.send("Something went wrong — try again in a moment.", ephemeral=True)
            except Exception:
                pass

    async def _turn_page(self, ctx: ComponentContext, custom_id: str):
        jump = False
        nav = cards.parse_nav_custom_id(custom_id)
        me = cards.parse_me_custom_id(custom_id)
        if nav is not None:
            group_id, page, period = nav
        elif me is not None:
            (group_id, period), page, jump = me, 1, True
        else:
            group_id = cards.parse_period_select_id(custom_id)
            values = list(getattr(ctx, "values", None) or [])
            if group_id is None or not values:
                return
            period, page = str(values[0]), 1

        # Deferred first: the board is several aggregate reads, and Discord
        # shows "This interaction failed" past 3s even when the edit lands.
        await ctx.defer(edit_origin=True)
        view = await asyncio.to_thread(
            _load_board, ctx.guild_id, ctx.author.id, period, page, jump_to_viewer=jump
        )
        if view.get("error"):
            return await ctx.edit_origin(content=view["error"], embeds=[], components=[])
        # The id names a clan; the reply is only ever drawn for the clan this
        # server is linked to. A mismatch means the link changed under the
        # message -- redraw for the current clan rather than trusting the id.
        if view["group_id"] != group_id:
            view = await asyncio.to_thread(_load_board, ctx.guild_id, ctx.author.id, "all", 1)
            if view.get("error"):
                return await ctx.edit_origin(content=view["error"], embeds=[], components=[])
        embed, components = _board_message(view)
        await ctx.edit_origin(embeds=[embed], components=components)

