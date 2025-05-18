from datetime import datetime
import os
import interactions
from interactions import ChannelType, Extension, listen, Message
from interactions.api.events import MessageCreate, Component
from db.models import Group, GroupPatreon, PlayerPet, session, Player, ItemList, PersonalBestEntry
from db.update_player_total import update_player_in_redis
from db.xf.recent_submissions import create_xenforo_entry
from utils.embeds import update_boss_pb_embed
from utils.messages import confirm_new_npc, confirm_new_item, name_change_message, new_player_message
from utils.msg_logger import HighThroughputLogger
from data.submissions import clog_processor, ca_processor, pb_processor, drop_processor
from utils.ge_value import get_true_item_value
from utils.format import convert_to_ms, convert_from_ms, get_true_boss_name
from utils.redis import redis_client
from db.app_logger import AppLogger



app_logger = AppLogger()
bot_token = os.getenv("DISCORD_TOKEN")
ignored_list = []
last_xf_transfer = datetime.now()


class MessageHandler(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot


    @listen(MessageCreate)
    async def on_message_create(self, event: MessageCreate):
        global last_xf_transfer
        global ignored_list
        bot: interactions.Client = event.bot
        if bot.is_closed:
            await bot.astart(token=bot_token)
        await bot.wait_until_ready()
        if type(event) == Message:
            message = event
        else:
            message = event.message
        if message.author.system:  # or message.author.bot:
            return
        if message.author.id == bot.user.id:
            return
        if message.channel.type == ChannelType.DM or message.channel.type == ChannelType.GROUP_DM:
            return
        

        channel_id = message.channel.id

        # if str(message.channel.id) == "1262137292315688991":
        skip_check = True
        target_guilds = ["1172737525069135962",
                        "900855778095800380",
                        "597397938989432842",
                        "702992720909828168",
                        "1120606216972947468"]
        if str(message.guild.id) in target_guilds:
            #print(f"Guild name: {message.guild.name}")
        #if str(channel_id) == "1262137292315688991":
            item_name = ""
            player_name = ""
            item_id = 0
            npc_name = "none"
            value = 0
            quantity = 0
            sheet_id = ""
            source_type = ""
            imageUrl = ""
            token = ""
            account_hash = ""
            for embed in message.embeds:
                field_names = [field.name for field in embed.fields]
                if "type" in field_names:
                    field_values = [field.value.lower().strip() for field in embed.fields]
                    if "source" in field_names and "loot chest" in field_values:
                        ## Skip pvp
                        continue
                    rsn = ""
                    if "collection_log" in field_values:
                        reported_slots = 1
                        for field in embed.fields:
                            if field.name == "item":
                                item_name = field.value
                            if field.name == "auth_key":
                                token = field.value
                            elif field.name == "player":
                                rsn = field.value
                            elif field.name == "item_id":
                                itemId = field.value
                            elif field.name == "source":
                                npcName = field.value
                            elif field.name == "acc_hash":
                                account_hash = field.value
                            elif field.name == "slots":
                                #print("Slots field:", field.value)
                                max_slots, reported_slots = field.value.split("/")
                                reported_slots = reported_slots.replace("/","")
                                max_slots = max_slots.replace("/","")
                                #print("reported, max slots:", reported_slots, "/",max_slots)
                            elif field.name == "rarity":
                                if field.value != "OptionalDouble.empty":
                                    rarity = field.value
                                else:
                                    rarity = ""
                            elif field.name == "sheet":
                                sheet_id = field.value
                            elif field.name == "kc":
                                if field.value != "null":
                                    kc = field.value
                                else:
                                    kc = 0

                        imageUrl = ""
                        if rsn == "":
                            return
                        attachment_url = None
                        attachment_type = None
                        if message.attachments:
                            for attachment in message.attachments:
                                if attachment.url:
                                    attachment_url = attachment.url
                                    attachment_type = attachment.content_type
                        clog_data = {
                            'player_name': rsn,
                            'acc_hash': account_hash,
                            'auth_key': token,
                            'item_name': item_name,
                            'source': npcName,
                            'kc': kc,
                            'reported_slots': reported_slots,
                            'attachment_url': attachment_url,
                            'attachment_type': attachment_type,
                            'message_id': message.id,
                            'channel_id': message.channel.id,
                        }
                        await clog_processor(clog_data)
                        continue
                    elif "combat_achievement" in field_values:
                        if embed.fields:
                            acc_hash, task_type, points_awarded, points_total, completed_tier, auth_key = None, None, None, None, None, None
                            task_tier = None
                            for field in embed.fields:
                                if field.name == "acc_hash":
                                    acc_hash = field.value
                                elif field.name == "points":
                                    points_awarded = field.value
                                elif field.name == "total_points":
                                    points_total = field.value
                                elif field.name == "completed":
                                    completed_tier = field.value
                                elif field.name == "auth_key":
                                    auth_key = field.value
                                elif field.name == "task":
                                    task_name = field.value
                                elif field.name == "tier":
                                    task_tier = field.value
                                elif field.name == "player_name":
                                    player_name = field.value
                            attachment_url = None
                            attachment_type = None
                            if message.attachments:
                                for attachment in message.attachments:
                                    if attachment.url:
                                        attachment_url = attachment.url
                                        attachment_type = attachment.content_type
                            ca_data = {
                                'acc_hash': acc_hash,
                                'player_name': player_name,
                                'auth_key': auth_key,
                                'task': task_name,
                                'tier': task_tier,
                                'points': points_awarded,
                                'total_points': points_total,
                                'completed': completed_tier,
                                'attachment_url': attachment_url,
                                'attachment_type': attachment_type,
                                'message_id': message.id,
                                'channel_id': message.channel.id
                            }
                            print("Calling ca_processor with data:", ca_data)
                            await ca_processor(ca_data)
                    elif "npc_kill" in field_values or "kill_time" in field_values:
                        npc_name = ""
                        current_time = ""
                        personal_best = ""
                        account_hash = ""
                        team_size = "Solo"
                        # print("npc_kill detected")
                        if embed.fields:
                            for field in embed.fields:
                                if field.name == "boss_name":
                                    npc_name = field.value
                                elif field.name == "player_name":
                                    player_name = field.value
                                if field.name == "auth_key":
                                    token = field.value
                                elif field.name == "acc_hash":
                                    account_hash = field.value
                                elif field.name == "kill_time":
                                    current_time = field.value
                                    current_time_ms = convert_to_ms(current_time)
                                elif field.name == "best_time":
                                    personal_best = field.value
                                    personal_best_ms = convert_to_ms(personal_best)
                                elif field.name == "is_pb":
                                    is_new_pb = False if field.value == "false" else True 
                                    if is_new_pb:
                                        ## A new PB sends no "pb", but instead a true boolean defining if the current_time is a new pb.
                                        personal_best_ms = current_time_ms
                                elif field.name == "team_size":
                                    team_size = field.value
                            attachment_url = None
                            attachment_type = None
                            if message.attachments:
                                for attachment in message.attachments:
                                    if attachment.url:
                                        attachment_url = attachment.url
                                        attachment_type = attachment.content_type
                            pb_data = {
                                'player_name': player_name,
                                'acc_hash': account_hash,
                                'auth_key': token,
                                'npc_name': npc_name,
                                'current_time_ms': current_time_ms, 
                                'personal_best_ms': personal_best_ms,
                                'team_size': team_size,
                                'is_new_pb': is_new_pb,
                                'attachment_url': attachment_url,
                                'attachment_type': attachment_type,
                                'message_id': message.id,
                                'channel_id': message.channel.id,
                            }
                            await pb_processor(pb_data)
                    elif embed.title and "received some drops" in embed.title or "drop" in field_values:
                        if embed.fields:
                            for field in embed.fields:
                                if field.name == "player":
                                    player_name = field.value.strip()
                                elif field.name == "item":
                                    item_name = field.value.strip()
                                elif field.name == "acc_hash":
                                    account_hash = field.value.strip()
                                elif field.name == "id":
                                    item_id = int(field.value.strip())
                                if field.name == "auth_key":
                                    token = field.value
                                elif field.name == "source":
                                    npc_name = field.value.strip()
                                    if npc_name in ignored_list:
                                        return
                                elif field.name == "value":
                                    if field.value:
                                        value = int(field.value)
                                    else:
                                        value = 0
                                elif field.name == "quantity":
                                    if field.value:
                                        quantity = int(field.value)
                                    else:
                                        quantity = 1
                                elif field.name == "sheet_id" or field.name == "sheet":
                                    sheet_id = field.value
                                elif field.name == "webhook" and len(field.value) > 10:
                                    pass
                            item_value = await get_true_item_value(item_name, value)
                            attachment_url = None
                            attachment_type = None
                            if message.attachments:
                                for attachment in message.attachments:
                                    if attachment.url:
                                        attachment_url = attachment.url
                                        attachment_type = attachment.content_type
                        drop_data = {"npc_name": npc_name,
                                    'item_name': item_name,
                                    'acc_hash': account_hash,
                                    'auth_key': token,
                                    'value': item_value,
                                    'quantity': quantity,
                                    'player_name': player_name,
                                    'item_id': item_id,
                                    'attachment_url': attachment_url,
                                    'attachment_type': attachment_type}
                        # print("Sending drop data:", drop_data)
                        await drop_processor(drop_data)
                            
                        continue
                    elif "adventure_log" in field_values:
                        if embed.fields:
                            for field in embed.fields:
                                if field.name == "player":
                                    player_name = field.value
                                    break
                        player_object = session.query(Player).filter(Player.player_name == player_name).first()
                        if player_object:
                            player_id = player_object.player_id
                        else:
                            continue
                        if embed.fields:
                            for field in embed.fields:
                                if field.name == "player":
                                    player_name = field.value
                                elif field.name == "acc_hash":
                                    account_hash = field.value
                                if field.name != "type" and field.name != "player" and field.name != "acc_hash":
                                    try:
                                        field_int = int(field.name)
                                        pb_content = field.value
                                        personal_bests = pb_content.split("\n")
                                        for pb in personal_bests:
                                            boss_name, rest = pb.split(" - ")
                                            team_size, time = rest.split(" : ")
                                            boss_name = boss_name.strip()
                                            team_size = team_size.strip()
                                            boss_name, team_size, time = boss_name.replace("`", ""), team_size.replace("`", ""), time.replace("`", "")
                                            time = time.strip()
                                            real_boss_name, npc_id = get_true_boss_name(boss_name)
                                            existing_pb = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id == player_id, PersonalBestEntry.npc_id == npc_id,
                                                                                                PersonalBestEntry.team_size == team_size).first()
                                            time_ms = convert_to_ms(time)
                                            if existing_pb:
                                                if time_ms < existing_pb.personal_best:
                                                    existing_pb.personal_best = time_ms
                                                    session.commit()
                                            else:
                                                new_pb = PersonalBestEntry(player_id=player_id, npc_id=npc_id, 
                                                                        team_size=team_size, personal_best=time_ms, 
                                                                        kill_time=time_ms, new_pb=True)
                                                session.add(new_pb)
                                                session.commit()
                                    
                                    except ValueError:
                                        pet_list = field.value
                                        pet_list = pet_list.replace("[", "")
                                        pet_list = pet_list.replace("]", "")
                                        pet_list = pet_list.split(",")
                                        if len(pet_list) > 0:
                                            for pet in pet_list:
                                                pet = int(pet.strip())
                                                item_object: ItemList = session.query(ItemList).filter(ItemList.item_id == pet).first()
                                                if item_object:
                                                    player_pet = PlayerPet(player_id=player_id, item_id=item_object.item_id, pet_name=item_object.item_name)
                                                    try:
                                                        session.add(player_pet)
                                                        session.commit()
                                                        print("Added a pet to the database for", player_name, account_hash, item_object.item_name, item_object.item_id)
                                                    except Exception as e:
                                                        print("Couldn't add a pet to the database:", e)
                                                        session.rollback()

    
    @listen(Component)
    async def on_component(event: Component):
        ctx = event.ctx
        custom_id = ctx.custom_id
        if custom_id.startswith("patreon_group_"):
            group_id = int(custom_id.split("_")[2])
            valid_patreon = session.query(GroupPatreon).filter(GroupPatreon.user_id == ctx.user.id).first()
            if valid_patreon and valid_patreon.group_id == None:
                valid_patreon.group_id = group_id
                group = session.query(Group).filter(Group.group_id == group_id).first()
                await ctx.send(f"You have assigned your DropTracker Patreon subscription perks to {group.group_name}!")
                try:
                    await ctx.message.delete()
                except Exception as e:
                    print("Couldn't delete the message:", e)
                return
            else:
                await ctx.send("You don't have a valid Patreon subscription, or you already have a group assigned to it.")
                return