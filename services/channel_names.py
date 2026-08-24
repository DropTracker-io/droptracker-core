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
import time
import asyncio

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
                #print("Got all loot channel id configs", loot_channel_id_configs)
                for channel_setting in loot_channel_id_configs:
                    #print("Channel setting is:", channel_setting)
                    if channel_setting.config_value:
                        #print("Channel setting value is not empty")
                        try:
                            channel = await bot.fetch_channel(channel_id=channel_setting.config_value)
                            if channel:
                                #print("Channel is not None")
                                if channel.type == ChannelType.GUILD_VOICE:
                                    #print("Channel is a voice channel")
                                    template = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_monthly_loot_text',
                                                                                        GroupConfiguration.group_id == channel_setting.group_id).first()
                                    template_str = template.config_value if template else ""
                                    if template_str == "" or not template_str:
                                        template_str = "{month}: {gp_amount} gp"
                                    # Show the number the lootboard drew, don't re-derive it.
                                    # This used to sum every WOM member's global monthly total,
                                    # which ignored the board's ignored_players, drop-moderation
                                    # exclusions and split-GP credits — group 14's channel read
                                    # 984.6M gp higher than its own board (suggestion #138).
                                    partition = get_current_partition()
                                    group_total = board_month_total(channel_setting.group_id, partition)
                                    if group_total is None:
                                        # No board rendered for this partition yet (new group, or
                                        # the first minutes of a new month). Leave the channel name
                                        # alone rather than posting a total the board disagrees with.
                                        continue
                                    month_str = datetime.now().strftime("%B")
                                    fin_text = template_str.replace("{month}", month_str).replace("{gp_amount}", format_number(group_total))
                                    await channel.edit(name=f"{fin_text}")
                            else:
                                continue
                                #print("Channel is not found for group ID", channel_setting.group_id, "and config value", channel_setting.config_value)
                        except Exception as e:
                            print("Couldn't edit the channel. e:", e)
                member_channel_id_configs = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_droptracker_users',
                                                                                    GroupConfiguration.config_value != "").all()
                print("Updating group member channel names for", len(member_channel_id_configs), "channels")
                for channel_setting in member_channel_id_configs:
                    try:
                        if channel_setting.group_id == 2:
                            total_members = session.query(Player.player_id).count()
                        else:
                            group = session.query(Group).filter(Group.group_id == channel_setting.group_id).first()
                            total_members = group.get_player_count()
                        if channel_setting.config_value != "":
                            channel = await bot.fetch_channel(channel_id=channel_setting.config_value)
                            template = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_droptracker_users_text',
                                                                                GroupConfiguration.group_id == channel_setting.group_id).first()
                            template_str = template.config_value if template else ""
                            if template_str == "" or not template_str:
                                template_str = "{member_count} members"
                            if channel:
                                await channel.edit(name=template_str.replace("{member_count}", str(total_members)))
                    except Exception as e:
                        print("Couldn't edit the channel. e:", e)
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
