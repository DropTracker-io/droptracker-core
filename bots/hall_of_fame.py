import interactions
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from interactions.api.events import MessageCreate, Startup
from interactions import Embed, Intents, Message, ChannelType, OptionType, slash_command, Permissions, slash_option
from db.models import Group, ItemList, PersonalBestEntry, PlayerPet, Session, Player, User, GroupConfiguration
from utils.format import convert_to_ms, get_true_boss_name
from services import hall_of_fame
import time


load_dotenv()

bot = interactions.Client(token=os.getenv("HALL_OF_FAME_BOT_TOKEN"), intents=Intents.ALL)


@interactions.listen(Startup)
async def on_startup(event: Startup):
    print("Hall of Fame bot started.")
    try:
        local_session = Session()
        groups_to_update = local_session.query(GroupConfiguration.group_id).filter(GroupConfiguration.config_key == "create_pb_embeds",
                                                                                 GroupConfiguration.config_value == "1").all()
        total_groups = len(groups_to_update)
    except Exception as e:
        print("Error getting groups to update:", e)
        return
    bot.load_extension("services.hall_of_fame")
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f"{total_groups} Halls of Fame", type=interactions.ActivityType.WATCHING))

if __name__ == "__main__":
    bot.start()