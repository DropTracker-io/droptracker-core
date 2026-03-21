"""
Discord user-context-menu extension for inspecting player accounts.

Admins can right-click any member in the server and choose "Player Lookup"
to see whether that member has a registered DropTracker account, which OSRS
players belong to them, monthly loot totals, points standing, and whether
they are submitting via the API or the RuneLite plugin.
"""

from datetime import datetime

import interactions
from interactions import (
    Embed,
    EmbedField,
    Extension,
    Member,
    ContextMenuContext,
    Permissions,
    user_context_menu,
    slash_default_member_permission,
)
from sqlalchemy import func as sa_func, desc

from db.models import (
    Drop,
    CollectionLogEntry,
    CombatAchievementEntry,
    Group,
    GroupConfiguration,
    Guild,
    PersonalBestEntry,
    Player,
    PlayerPoints,
    QuestCompletionEntry,
    Session,
    User,
    get_current_partition,
    user_group_association,
)
from services.redis_updates import get_player_current_month_total
from utils.format import format_number


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_session():
    return Session()


def _format_gp(value: int) -> str:
    return f"{value:,} gp"


def _check_points_active(group_id: int, db) -> bool:
    """Return True if the group has a points system active (premium tier >= 2)."""
    from sqlalchemy import text
    try:
        row = db.execute(
            text(
                "SELECT 1 FROM xenforo.xf_dt_group_upgrade_active "
                "WHERE group_id = :gid AND is_cancelled = 0 AND group_upgrade_id >= 2 "
                "LIMIT 1"
            ),
            {"gid": group_id},
        ).first()
        return row is not None
    except Exception:
        return False


def _detect_api_usage(player_id: int, db, limit: int = 20):
    """
    Sample the most recent submissions for a player and return
    (api_count, total_checked) so callers can describe usage.
    """
    recent_drops = (
        db.query(Drop.used_api)
        .filter(Drop.player_id == player_id)
        .order_by(desc(Drop.date_added))
        .limit(limit)
        .all()
    )
    recent_clogs = (
        db.query(CollectionLogEntry.used_api)
        .filter(CollectionLogEntry.player_id == player_id)
        .order_by(desc(CollectionLogEntry.date_added))
        .limit(limit)
        .all()
    )
    recent_pbs = (
        db.query(PersonalBestEntry.used_api)
        .filter(PersonalBestEntry.player_id == player_id)
        .order_by(desc(PersonalBestEntry.date_added))
        .limit(limit)
        .all()
    )
    recent_cas = (
        db.query(CombatAchievementEntry.used_api)
        .filter(CombatAchievementEntry.player_id == player_id)
        .order_by(desc(CombatAchievementEntry.date_added))
        .limit(limit)
        .all()
    )
    recent_quests = (
        db.query(QuestCompletionEntry.used_api)
        .filter(QuestCompletionEntry.player_id == player_id)
        .order_by(desc(QuestCompletionEntry.date_added))
        .limit(limit)
        .all()
    )
    all_rows = recent_drops + recent_clogs + recent_pbs + recent_cas + recent_quests
    if not all_rows:
        return 0, 0
    api_count = sum(1 for (flag,) in all_rows if flag)
    return api_count, len(all_rows)


def _api_usage_label(api_count: int, total: int) -> str:
    if total == 0:
        return "No submissions"
    ratio = api_count / total
    if ratio >= 0.9:
        return "API"
    if ratio <= 0.1:
        return "RuneLite Plugin"
    return f"Mixed ({api_count}/{total} via API)"


def _player_total_points(player_id: int, group_id: int, db) -> int:
    total = (
        db.query(sa_func.coalesce(sa_func.sum(PlayerPoints.amount), 0))
        .filter(
            PlayerPoints.player_id == player_id,
            PlayerPoints.group_id == group_id,
        )
        .scalar()
    )
    return int(total)


def _player_submission_counts(player_id: int, partition: int, db) -> dict:
    """Return per-type submission counts for the current month partition."""
    drops = (
        db.query(sa_func.count(Drop.drop_id))
        .filter(Drop.player_id == player_id, Drop.partition == partition)
        .scalar()
    ) or 0
    clogs = (
        db.query(sa_func.count(CollectionLogEntry.log_id))
        .filter(
            CollectionLogEntry.player_id == player_id,
            sa_func.extract("year", CollectionLogEntry.date_added) * 100
            + sa_func.extract("month", CollectionLogEntry.date_added)
            == partition,
        )
        .scalar()
    ) or 0
    pbs = (
        db.query(sa_func.count(PersonalBestEntry.id))
        .filter(
            PersonalBestEntry.player_id == player_id,
            sa_func.extract("year", PersonalBestEntry.date_added) * 100
            + sa_func.extract("month", PersonalBestEntry.date_added)
            == partition,
        )
        .scalar()
    ) or 0
    cas = (
        db.query(sa_func.count(CombatAchievementEntry.id))
        .filter(
            CombatAchievementEntry.player_id == player_id,
            sa_func.extract("year", CombatAchievementEntry.date_added) * 100
            + sa_func.extract("month", CombatAchievementEntry.date_added)
            == partition,
        )
        .scalar()
    ) or 0
    return {"drops": drops, "clogs": clogs, "pbs": pbs, "cas": cas}


def _build_player_field(
    player: Player,
    group_id: int,
    points_active: bool,
    partition: int,
    db,
) -> EmbedField:
    """Build one embed field summarising a single player account."""
    month_total = get_player_current_month_total(player.player_id)
    counts = _player_submission_counts(player.player_id, partition, db)
    api_count, api_total = _detect_api_usage(player.player_id, db, limit=20)

    lines = []
    lines.append(f"**Monthly Loot:** {format_number(month_total)}")

    submissions_parts = []
    if counts["drops"]:
        submissions_parts.append(f"{counts['drops']} drops")
    if counts["clogs"]:
        submissions_parts.append(f"{counts['clogs']} clogs")
    if counts["pbs"]:
        submissions_parts.append(f"{counts['pbs']} PBs")
    if counts["cas"]:
        submissions_parts.append(f"{counts['cas']} CAs")
    if submissions_parts:
        lines.append(f"**This Month:** {', '.join(submissions_parts)}")
    else:
        lines.append("**This Month:** No submissions")

    lines.append(f"**Source:** {_api_usage_label(api_count, api_total)}")

    if player.total_level:
        lines.append(f"**Total Level:** {player.total_level:,}")
    if player.log_slots:
        lines.append(f"**Col. Log Slots:** {player.log_slots:,}")

    if points_active:
        pts = _player_total_points(player.player_id, group_id, db)
        lines.append(f"**Points:** {pts:,}")

    return EmbedField(
        name=f"\u2694\ufe0f {player.player_name}  (ID: {player.player_id})",
        value="\n".join(lines),
        inline=False,
    )


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

class UserContextMenu(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        print("[UserContextMenu] Extension loaded.")

    @user_context_menu(name="Player Lookup")
    @slash_default_member_permission(Permissions.ADMINISTRATOR)
    async def player_lookup(self, ctx: ContextMenuContext):
        member: Member = ctx.target
        await ctx.defer(ephemeral=True)

        db = _get_session()
        try:
            guild_row = (
                db.query(Guild)
                .filter(Guild.guild_id == str(ctx.guild_id))
                .first()
            )
            if not guild_row:
                await ctx.send("This server is not linked to a DropTracker group.", ephemeral=True)
                return

            group: Group = guild_row.group
            group_id = group.group_id

            user = (
                db.query(User)
                .filter(User.discord_id == str(member.id))
                .first()
            )
            if not user:
                embed = Embed(
                    title="Player Lookup",
                    description=(
                        f"**{member.display_name}** does not have a registered "
                        f"DropTracker account."
                    ),
                    color=0xE74C3C,
                )
                embed.set_thumbnail(url=member.avatar.url if member.avatar else "")
                await ctx.send(embeds=[embed], ephemeral=True)
                return

            players = db.query(Player).filter(Player.user_id == user.user_id).all()

            in_group_players = []
            other_players = []
            group_player_ids = {
                row.player_id
                for row in db.query(user_group_association.c.player_id)
                .filter(user_group_association.c.group_id == group_id)
                .all()
            }
            for p in players:
                if p.player_id in group_player_ids:
                    in_group_players.append(p)
                else:
                    other_players.append(p)

            partition = get_current_partition()
            points_active = _check_points_active(group_id, db)

            embed = Embed(
                title=f"Player Lookup — {member.display_name}",
                color=0x2ECC71 if in_group_players else 0xF39C12,
            )
            embed.set_thumbnail(url=member.avatar.url if member.avatar else "")

            account_lines = [f"**User ID:** {user.user_id}"]
            if user.username:
                account_lines.append(f"**RSN (primary):** {user.username}")
            account_lines.append(
                f"**Registered:** <t:{int(user.date_added.timestamp())}:R>"
                if user.date_added
                else "**Registered:** Unknown"
            )
            account_lines.append(f"**Players:** {len(players)}")
            account_lines.append(f"**In this group:** {len(in_group_players)}")

            if user.hidden:
                account_lines.append(f"**Hidden:** Yes")

            embed.add_field(
                name="Account Overview",
                value="\n".join(account_lines),
                inline=False,
            )

            if in_group_players:
                for p in in_group_players:
                    field = _build_player_field(p, group_id, points_active, partition, db)
                    embed.add_field(name=field.name, value=field.value, inline=field.inline)
            else:
                embed.add_field(
                    name="Group Membership",
                    value=(
                        f"None of this user's {len(players)} player(s) are "
                        f"registered in **{group.group_name}**."
                    ),
                    inline=False,
                )

            if other_players:
                names = ", ".join(p.player_name for p in other_players[:10])
                suffix = f" (+{len(other_players) - 10} more)" if len(other_players) > 10 else ""
                embed.add_field(
                    name="Other Players (not in this group)",
                    value=f"{names}{suffix}",
                    inline=False,
                )

            if points_active and in_group_players:
                total_pts = sum(
                    _player_total_points(p.player_id, group_id, db)
                    for p in in_group_players
                )
                embed.set_footer(
                    text=f"Combined group points: {total_pts:,} | {group.group_name}"
                )
            else:
                embed.set_footer(text=group.group_name)

            await ctx.send(embeds=[embed], ephemeral=True)

        except Exception as exc:
            import traceback
            traceback.print_exc()
            await ctx.send(
                f"An error occurred during lookup: {type(exc).__name__}",
                ephemeral=True,
            )
        finally:
            db.close()
