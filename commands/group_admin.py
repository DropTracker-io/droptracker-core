"""
Group Admin Commands Module

Contains slash commands for authorized group admins to manually adjust
group-specific points for their members, with full audit logging.

Authorization requires one of:
  - Discord ADMINISTRATOR permission in the guild
  - Discord ID listed in the group's `authed_users` GroupConfiguration entry

Classes:
    GroupAdminCommands: Extension containing group admin point management commands

Author: joelhalen
"""

import json
import time
from datetime import datetime

from interactions import (
    AutocompleteContext, SlashContext, Embed, OptionType,
    Extension, slash_command, slash_option,
)
from sqlalchemy import desc

from db.models import (
    session, User, Group, Guild, Player, PlayerPoints, Log,
)
from .utils import is_admin, is_user_authorized

# entry_type sentinel that marks a PlayerPoints row as an admin manual adjustment
ADMIN_MANUAL_ENTRY_TYPE = 99


class GroupAdminCommands(Extension):
    """
    Extension containing group admin commands for managing member points.

    Commands require the invoking user to be a Discord guild administrator or
    to appear in the group's ``authed_users`` GroupConfiguration list.
    """

    def __init__(self, bot):
        self.bot = bot

    def _refresh_session(self):
        session.remove()

    def _get_group_for_guild(self, guild_id):
        if not guild_id:
            return None
        guild = session.query(Guild).filter(Guild.guild_id == str(guild_id)).first()
        if not guild or not guild.group_id:
            return None
        return session.query(Group).filter(Group.group_id == guild.group_id).first()

    def _player_in_group(self, player: Player, group: Group) -> bool:
        return group in player.groups

    def _write_audit_log(
        self,
        action: str,
        actor_discord_id,
        actor_user_id,
        player_name: str,
        player_id: int,
        group_name: str,
        group_id: int,
        amount: int,
        reason: str,
        entry_id: int,
    ):
        details = json.dumps({
            "action": action,
            "actor_discord_id": str(actor_discord_id),
            "actor_user_id": actor_user_id,
            "player_name": player_name,
            "player_id": player_id,
            "group_name": group_name,
            "group_id": group_id,
            "amount": amount,
            "reason": reason,
            "entry_id": entry_id,
        })
        log_entry = Log(
            level="INFO",
            source="group_admin_points",
            message=(
                f"{action.upper()}: <@{actor_discord_id}> adjusted "
                f"{player_name}'s points by {amount:+d} in {group_name} "
                f"(reason: {reason})"
            ),
            details=details,
            timestamp=int(time.time()),
        )
        session.add(log_entry)

    # ------------------------------------------------------------------
    # /add-group-points
    # ------------------------------------------------------------------

    @slash_command(
        name="add-group-points",
        description="Add points to a group member's balance (group admins only)",
    )
    @slash_option(
        name="player",
        description="RuneScape account name of the group member",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
    )
    @slash_option(
        name="amount",
        description="Number of points to add (1 – 1,000,000)",
        opt_type=OptionType.INTEGER,
        required=True,
        min_value=1,
        max_value=1_000_000,
    )
    @slash_option(
        name="reason",
        description="Reason for this manual adjustment (3 – 120 characters)",
        opt_type=OptionType.STRING,
        required=True,
        min_length=3,
        max_length=120,
    )
    async def add_group_points_cmd(
        self, ctx: SlashContext, player: str, amount: int, reason: str
    ):
        self._refresh_session()

        if not ctx.guild_id:
            return await ctx.send(
                "This command must be used inside your group's Discord server.",
                ephemeral=True,
            )

        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(
                "This Discord server is not linked to a DropTracker group.",
                ephemeral=True,
            )

        actor = session.query(User).filter(
            User.discord_id == str(ctx.author.id)
        ).first()
        if not await is_admin(ctx) and not (actor and is_user_authorized(actor.user_id, group)):
            return await ctx.send(
                "You are not authorized to manage points for this group.",
                ephemeral=True,
            )

        target = session.query(Player).filter(
            Player.player_name.ilike(player.strip())
        ).first()
        if not target:
            return await ctx.send(
                f"No player named `{player}` was found in the database.",
                ephemeral=True,
            )

        if not self._player_in_group(target, group):
            return await ctx.send(
                f"`{target.player_name}` is not a member of **{group.group_name}**.",
                ephemeral=True,
            )

        try:
            entry = PlayerPoints(
                player_id=target.player_id,
                group_id=group.group_id,
                amount=amount,
                reason=f"[Admin] {reason}"[:125],
                entry_type=ADMIN_MANUAL_ENTRY_TYPE,
            )
            session.add(entry)
            session.flush()  # populate entry.id before writing audit log

            self._write_audit_log(
                action="add",
                actor_discord_id=ctx.author.id,
                actor_user_id=actor.user_id if actor else None,
                player_name=target.player_name,
                player_id=target.player_id,
                group_name=group.group_name,
                group_id=group.group_id,
                amount=amount,
                reason=reason,
                entry_id=entry.id,
            )
            session.commit()
        except Exception as e:
            session.rollback()
            return await ctx.send(f"Failed to add points: {e}", ephemeral=True)

        embed = Embed(
            title="Points Added",
            description=(
                f"Added **{amount:,}** point(s) to `{target.player_name}` "
                f"in **{group.group_name}**."
            ),
            color=0x2ECC71,
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Entry ID", value=f"`#{entry.id}`", inline=True)
        embed.add_field(name="Performed by", value=f"<@{ctx.author.id}>", inline=True)
        embed.set_footer(text="This adjustment has been recorded in the audit log.")
        await ctx.send(embed=embed, ephemeral=True)

    @add_group_points_cmd.autocomplete("player")
    async def add_group_points_autocomplete(self, ctx: AutocompleteContext):
        self._refresh_session()
        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(choices=[])

        query = (ctx.input_text or "").strip().lower()
        players = group.get_players()
        if query:
            players = [p for p in players if query in p.player_name.lower()]

        await ctx.send(choices=[
            {"name": p.player_name, "value": p.player_name}
            for p in players[:25]
        ])

    # ------------------------------------------------------------------
    # /remove-group-points
    # ------------------------------------------------------------------

    @slash_command(
        name="remove-group-points",
        description="Remove points from a group member's balance (group admins only)",
    )
    @slash_option(
        name="player",
        description="RuneScape account name of the group member",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
    )
    @slash_option(
        name="amount",
        description="Number of points to remove (1 – 1,000,000)",
        opt_type=OptionType.INTEGER,
        required=True,
        min_value=1,
        max_value=1_000_000,
    )
    @slash_option(
        name="reason",
        description="Reason for this manual adjustment (3 – 120 characters)",
        opt_type=OptionType.STRING,
        required=True,
        min_length=3,
        max_length=120,
    )
    async def remove_group_points_cmd(
        self, ctx: SlashContext, player: str, amount: int, reason: str
    ):
        self._refresh_session()

        if not ctx.guild_id:
            return await ctx.send(
                "This command must be used inside your group's Discord server.",
                ephemeral=True,
            )

        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(
                "This Discord server is not linked to a DropTracker group.",
                ephemeral=True,
            )

        actor = session.query(User).filter(
            User.discord_id == str(ctx.author.id)
        ).first()
        if not await is_admin(ctx) and not (actor and is_user_authorized(actor.user_id, group)):
            return await ctx.send(
                "You are not authorized to manage points for this group.",
                ephemeral=True,
            )

        target = session.query(Player).filter(
            Player.player_name.ilike(player.strip())
        ).first()
        if not target:
            return await ctx.send(
                f"No player named `{player}` was found in the database.",
                ephemeral=True,
            )

        if not self._player_in_group(target, group):
            return await ctx.send(
                f"`{target.player_name}` is not a member of **{group.group_name}**.",
                ephemeral=True,
            )

        try:
            entry = PlayerPoints(
                player_id=target.player_id,
                group_id=group.group_id,
                amount=-amount,
                reason=f"[Admin] {reason}"[:125],
                entry_type=ADMIN_MANUAL_ENTRY_TYPE,
            )
            session.add(entry)
            session.flush()

            self._write_audit_log(
                action="remove",
                actor_discord_id=ctx.author.id,
                actor_user_id=actor.user_id if actor else None,
                player_name=target.player_name,
                player_id=target.player_id,
                group_name=group.group_name,
                group_id=group.group_id,
                amount=-amount,
                reason=reason,
                entry_id=entry.id,
            )
            session.commit()
        except Exception as e:
            session.rollback()
            return await ctx.send(f"Failed to remove points: {e}", ephemeral=True)

        embed = Embed(
            title="Points Removed",
            description=(
                f"Removed **{amount:,}** point(s) from `{target.player_name}` "
                f"in **{group.group_name}**."
            ),
            color=0xE74C3C,
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Entry ID", value=f"`#{entry.id}`", inline=True)
        embed.add_field(name="Performed by", value=f"<@{ctx.author.id}>", inline=True)
        embed.set_footer(text="This adjustment has been recorded in the audit log.")
        await ctx.send(embed=embed, ephemeral=True)

    @remove_group_points_cmd.autocomplete("player")
    async def remove_group_points_autocomplete(self, ctx: AutocompleteContext):
        self._refresh_session()
        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(choices=[])

        query = (ctx.input_text or "").strip().lower()
        players = group.get_players()
        if query:
            players = [p for p in players if query in p.player_name.lower()]

        await ctx.send(choices=[
            {"name": p.player_name, "value": p.player_name}
            for p in players[:25]
        ])

    # ------------------------------------------------------------------
    # /group-points-audit
    # ------------------------------------------------------------------

    @slash_command(
        name="group-points-audit",
        description="View recent manual point adjustments for this group (group admins only)",
    )
    @slash_option(
        name="player",
        description="Filter by a specific group member (optional)",
        opt_type=OptionType.STRING,
        required=False,
        autocomplete=True,
    )
    async def group_points_audit_cmd(self, ctx: SlashContext, player: str = None):
        self._refresh_session()

        if not ctx.guild_id:
            return await ctx.send(
                "This command must be used inside your group's Discord server.",
                ephemeral=True,
            )

        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(
                "This Discord server is not linked to a DropTracker group.",
                ephemeral=True,
            )

        actor = session.query(User).filter(
            User.discord_id == str(ctx.author.id)
        ).first()
        if not await is_admin(ctx) and not (actor and is_user_authorized(actor.user_id, group)):
            return await ctx.send(
                "You are not authorized to view the audit log for this group.",
                ephemeral=True,
            )

        # Build query for manual adjustments on this group
        query = (
            session.query(PlayerPoints, Player)
            .join(Player, Player.player_id == PlayerPoints.player_id)
            .filter(
                PlayerPoints.group_id == group.group_id,
                PlayerPoints.entry_type == ADMIN_MANUAL_ENTRY_TYPE,
            )
        )

        if player:
            target = session.query(Player).filter(
                Player.player_name.ilike(player.strip())
            ).first()
            if target:
                query = query.filter(PlayerPoints.player_id == target.player_id)

        rows = query.order_by(desc(PlayerPoints.date_added)).limit(15).all()

        if not rows:
            filter_note = f" for `{player}`" if player else ""
            return await ctx.send(
                f"No manual point adjustments found{filter_note} in **{group.group_name}**.",
                ephemeral=True,
            )

        lines = []
        for pp, pl in rows:
            sign = "+" if pp.amount >= 0 else ""
            ts = int(pp.date_added.timestamp()) if pp.date_added else 0
            display_reason = pp.reason or "—"
            if display_reason.startswith("[Admin] "):
                display_reason = display_reason[len("[Admin] "):]
            lines.append(
                f"`#{pp.id}` <t:{ts}:f> — **{pl.player_name}** {sign}{pp.amount:,} pts\n"
                f"> {display_reason}"
            )

        filter_note = f" — `{player}`" if player else ""
        embed = Embed(
            title=f"Manual Point Adjustments — {group.group_name}{filter_note}",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(
            text=f"Showing up to 15 most recent entries. entry_type={ADMIN_MANUAL_ENTRY_TYPE}"
        )
        await ctx.send(embed=embed, ephemeral=True)

    @group_points_audit_cmd.autocomplete("player")
    async def group_points_audit_autocomplete(self, ctx: AutocompleteContext):
        self._refresh_session()
        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(choices=[])

        query = (ctx.input_text or "").strip().lower()
        players = group.get_players()
        if query:
            players = [p for p in players if query in p.player_name.lower()]

        await ctx.send(choices=[
            {"name": p.player_name, "value": p.player_name}
            for p in players[:25]
        ])
