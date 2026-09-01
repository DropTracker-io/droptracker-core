"""
    Handles updating channel names based on group member count/loot value
"""
import interactions
from interactions import Extension, Task, IntervalTrigger, ChannelType
from db.models import Group, GroupConfiguration, session, Player
from datetime import datetime, timedelta
from sqlalchemy import text
from utils.format import format_number, get_current_partition
from services.group_loot_totals import board_month_total
from services.channel_name_render import render_channel_name, resolve_channel_id
import time
import asyncio

# Renaming is the entire point of these two settings, so only channel kinds that
# live in the sidebar as a static label belong here. Stage channels rename and
# sit in the sidebar exactly like a voice channel, and the website's picker
# offers both. Gating on GUILD_VOICE alone meant a stage channel was selectable
# and then silently never updated.
RENAMEABLE_CHANNEL_TYPES = (ChannelType.GUILD_VOICE, ChannelType.GUILD_STAGE_VOICE)


class ChannelNames(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        asyncio.create_task(self.update_channel_names())
        print("Channel names service initialized.")

    async def update_channel_names(self):
        while True:
            # Guard the whole iteration: this task is started once via
            # asyncio.create_task in __init__ and has no supervisor to restart
            # it, so a single unexpected error (bad query, None group, WOM/Discord
            # hiccup) must never escape the while loop. Log it and fall through to
            # the finally block so the updater keeps running and doesn't busy-spin.
            try:
                bot: interactions.Client = self.bot
                loot_channel_id_configs = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_monthly_loot').all()
                for channel_setting in loot_channel_id_configs:
                    group_id = channel_setting.group_id
                    channel_id = resolve_channel_id(channel_setting.config_value)
                    if channel_id is None:
                        continue
                    try:
                        channel = await bot.fetch_channel(channel_id=channel_id)
                        if not channel:
                            continue
                        if channel.type not in RENAMEABLE_CHANNEL_TYPES:
                            print(f"Skipping loot channel {channel_id} for group {group_id}: "
                                  f"type {channel.type} is not a voice/stage channel")
                            continue
                        template = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_monthly_loot_text',
                                                                            GroupConfiguration.group_id == group_id).first()
                        # Show the number the lootboard drew, don't re-derive it.
                        # This used to sum every WOM member's global monthly total,
                        # which ignored the board's ignored_players, drop-moderation
                        # exclusions and split-GP credits — group 14's channel read
                        # 984.6M gp higher than its own board (suggestion #138).
                        partition = get_current_partition()
                        group_total = board_month_total(group_id, partition)
                        if group_total is None:
                            # No board rendered for this partition yet (new group, or
                            # the first minutes of a new month). Leave the channel name
                            # alone rather than posting a total the board disagrees with.
                            continue
                        fin_text = render_channel_name(
                            template.config_value if template else "",
                            "{month}: {gp_amount} gp",
                            "{gp_amount}",
                            {
                                "{month}": datetime.now().strftime("%B"),
                                "{gp_amount}": format_number(group_total),
                            },
                        )
                        await channel.edit(name=fin_text)
                    except Exception as e:
                        print(f"Couldn't edit loot channel {channel_id} for group {group_id}. e:", e)
                member_channel_id_configs = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_droptracker_users',
                                                                                    GroupConfiguration.config_value != "").all()
                # This loop runs after the loot loop above and renames last, so a
                # group that pointed BOTH counters at one channel only ever sees
                # its member count — the loot name is overwritten every cycle,
                # with no error and (after the first write) no guild audit entry
                # either. 22 groups were in that state on 2026-09-01. Nothing
                # here can fix it — they need a second voice channel, which the
                # website's config editor now warns about — so the map exists
                # only to make the collision greppable instead of silent.
                loot_channel_by_group = {
                    c.group_id: resolve_channel_id(c.config_value) for c in loot_channel_id_configs
                }
                print("Updating group member channel names for", len(member_channel_id_configs), "channels")
                for channel_setting in member_channel_id_configs:
                    group_id = channel_setting.group_id
                    channel_id = resolve_channel_id(channel_setting.config_value)
                    if channel_id is None:
                        continue
                    # Junk resolves to None on both sides, so an unconfigured or
                    # '0'-sentinel pair can never look like a collision here.
                    if loot_channel_by_group.get(group_id) == channel_id:
                        print(f"Voice counter collision for group {group_id}: channel {channel_id} is set as "
                              f"both vc_to_display_monthly_loot and vc_to_display_droptracker_users — "
                              f"the member count is written last, so the loot total never shows")
                    try:
                        if group_id == 2:
                            total_members = session.query(Player.player_id).count()
                        else:
                            group = session.query(Group).filter(Group.group_id == group_id).first()
                            total_members = group.get_player_count()
                        channel = await bot.fetch_channel(channel_id=channel_id)
                        if not channel:
                            continue
                        # This loop had no type gate at all while the loot loop
                        # above did, so it renamed whatever it was pointed at.
                        # Groups 286/287/301 ended up with *text* channels named
                        # `3-members`, `6-members` and `438-members` — Discord
                        # slugifies text-channel names, which is where the
                        # dashes came from. Renaming a group's real text channel
                        # every ten minutes is worse than not counting at all.
                        if channel.type not in RENAMEABLE_CHANNEL_TYPES:
                            print(f"Skipping member channel {channel_id} for group {group_id}: "
                                  f"type {channel.type} is not a voice/stage channel")
                            continue
                        template = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_droptracker_users_text',
                                                                            GroupConfiguration.group_id == group_id).first()
                        fin_text = render_channel_name(
                            template.config_value if template else "",
                            "{member_count} members",
                            "{member_count}",
                            {"{member_count}": str(total_members)},
                        )
                        await channel.edit(name=fin_text)
                    except Exception as e:
                        print(f"Couldn't edit member channel {channel_id} for group {group_id}. e:", e)
            except Exception as e:
                # Backstop so no unexpected error (top-level query failure, None
                # group, etc.) can ever kill the while-True updater loop.
                print("channel_names update loop iteration failed. e:", e)
            finally:
                # Release the scoped session before sleeping so this thread does not
                # hold an idle read transaction for the full interval — the config
                # and member-count reads above otherwise leave the shared scoped
                # session's connection checked out (2026-07-15 idle-transaction
                # leak family). Nothing is held across iterations, so remove() is safe.
                # In finally so cleanup + sleep run even when the iteration raised,
                # preventing a tight busy-spin on a persistent error.
                try:
                    session.remove()
                except Exception:
                    pass
                await asyncio.sleep(600)
