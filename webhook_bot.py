import interactions
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from interactions.api.events import MessageCreate, Startup
from interactions import Embed, Intents, Message, ChannelType, OptionType, slash_command, Permissions, slash_option
from db.models import Group, ItemList, PersonalBestEntry, PlayerPet, Session, Player, User, UserConfiguration
from data.submissions import clog_processor, ca_processor, pb_processor, drop_processor
from utils.format import convert_to_ms, get_true_boss_name
from services.ticket_system import Tickets
import time

channel_id_to_use = 1210765287591256084

load_dotenv()

bot = interactions.Client(token=os.getenv("WEBHOOK_TOKEN"), intents=Intents.ALL)

# Update log management system
# This system allows administrators to create, manage, and send update logs using JSON files
# Commands available:
# - /update-add: Add items to a draft update log
# - /update-view: Preview a draft update log
# - /update-list: List all available drafts
# - /update-send: Send a completed draft to the updates channel
# - /update-delete: Delete a draft
# - /update-log: Original command for quick one-time updates
UPDATE_LOGS_DIR = "/store/droptracker/disc/data/updates"

def get_update_file_path(version: str) -> str:
    """Get the file path for a specific update version"""
    return os.path.join(UPDATE_LOGS_DIR, f"update_{version.replace('.', '_')}.json")

def load_update_data(version: str) -> dict:
    """Load update data from JSON file"""
    file_path = get_update_file_path(version)
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
            # Migrate old format to new format if needed
            if "added" in data and isinstance(data["added"], list):
                # Convert old flat structure to categorized structure
                old_data = data.copy()
                data = {
                    "version": version,
                    "categories": {
                        "General": {
                            "added": old_data.get("added", []),
                            "removed": old_data.get("removed", []),
                            "changed": old_data.get("changed", []),
                            "notes": old_data.get("notes", [])
                        }
                    },
                    "created_at": old_data.get("created_at", datetime.now().isoformat()),
                    "last_modified": datetime.now().isoformat()
                }
                # Save the migrated data
                save_update_data(version, data)
            return data
    return {
        "version": version,
        "categories": {},
        "created_at": datetime.now().isoformat(),
        "last_modified": datetime.now().isoformat()
    }

def save_update_data(version: str, data: dict) -> None:
    """Save update data to JSON file"""
    data["last_modified"] = datetime.now().isoformat()
    file_path = get_update_file_path(version)
    os.makedirs(UPDATE_LOGS_DIR, exist_ok=True)
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=2)

def list_update_versions() -> list:
    """List all available update versions"""
    if not os.path.exists(UPDATE_LOGS_DIR):
        return []
    versions = []
    for file in os.listdir(UPDATE_LOGS_DIR):
        if file.startswith("update_") and file.endswith(".json"):
            version = file[7:-5].replace('_', '.')  # Remove "update_" and ".json", replace _ with .
            versions.append(version)
    return sorted(versions, reverse=True)  # Most recent first

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    def embed_to_dict(embed: Embed):
        if embed.fields:
            return {f.name: f.value for f in embed.fields}
        return {}
    bot: interactions.Client = event.bot
    if bot.is_closed:
        await bot.astart(token=os.getenv("WEBHOOK_TOKEN"))
    await bot.wait_until_ready()
    if isinstance(event, Message):
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
    target_guilds = ["1172737525069135962",
                    "900855778095800380",
                    "597397938989432842",
                    "702992720909828168",
                    "1120606216972947468"]
    if str(message.guild.id) in target_guilds:
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
            embed_data = embed_to_dict(embed)
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.url:
                        embed_data['attachment_url'] = attachment.url
                        embed_data['attachment_type'] = attachment.content_type
            field_names = [field.name for field in embed.fields]
            if embed_data:
                field_values = [field.value.lower().strip() for field in embed.fields]
                if "source_type" in field_names and "loot chest" in field_values:
                    ## Skip pvp
                    continue
                rsn = ""
                embed_data['used_api'] = False
                if "collection_log" in field_values:
                    await clog_processor(embed_data)
                    continue
                elif "combat_achievement" in field_values:
                    await ca_processor(embed_data)
                    continue
                elif "npc_kill" in field_values or "kill_time" in field_values:
                    await pb_processor(embed_data)
                    continue
                elif embed.title and "received some drops" in embed.title or "drop" in field_values:
                    await drop_processor(embed_data)
                    continue
                elif "experience_update" in field_values or "experience_milestone" in field_values or "level_up" in field_values:
                    #await experience_processor(embed_data)
                    continue
                elif "quest_completion" in field_values:
                    #await quest_processor(embed_data)
                    continue
                elif "adventure_log" in field_values:
                    if embed.fields:
                        for field in embed.fields:
                            if field.name == "player":
                                player_name = field.value
                                break
                    # Use local session for database operations
                    local_session = Session()
                    try:
                        player_object = local_session.query(Player).filter(Player.player_name == player_name).first()
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
                                            existing_pb = local_session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id == player_id, PersonalBestEntry.npc_id == npc_id,
                                                                                                PersonalBestEntry.team_size == team_size).first()
                                            time_ms = convert_to_ms(time)
                                            if existing_pb:
                                                if time_ms < existing_pb.personal_best:
                                                    existing_pb.personal_best = time_ms
                                                    local_session.commit()
                                            else:
                                                new_pb = PersonalBestEntry(player_id=player_id, npc_id=npc_id, 
                                                                        team_size=team_size, personal_best=time_ms, 
                                                                        kill_time=time_ms, new_pb=True)
                                                local_session.add(new_pb)
                                                local_session.commit()
                                    
                                    except ValueError:
                                        pet_list = field.value
                                        pet_list = pet_list.replace("[", "")
                                        pet_list = pet_list.replace("]", "")
                                        pet_list = pet_list.split(",")
                                        if len(pet_list) > 0:
                                            for pet in pet_list:
                                                pet = int(pet.strip())
                                                item_object: ItemList = local_session.query(ItemList).filter(ItemList.item_id == pet).first()
                                                if item_object:
                                                    player_pet = PlayerPet(player_id=player_id, item_id=item_object.item_id, pet_name=item_object.item_name)
                                                    try:
                                                        local_session.add(player_pet)
                                                        local_session.commit()
                                                        print("Added a pet to the database for", player_name, account_hash, item_object.item_name, item_object.item_id)
                                                    except Exception as e:
                                                        print("Couldn't add a pet to the database:", e)
                                                        local_session.rollback()
                    except Exception as e:
                        local_session.rollback()
                        print(f"Error processing adventure log: {e}")
                    finally:
                        local_session.close()


@interactions.listen(Startup)
async def on_startup(event: Startup):
    print("Webhook bot started.")
    local_session = Session()
    try:
        player_count = local_session.query(Player.player_id).count()
        print(f"Webhook bot started with {player_count} players")
        bot.load_extension("services.ticket_system")
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name=f" ~{player_count} players", type=interactions.ActivityType.WATCHING))
    except Exception as e:
        print(f"Error during startup: {e}")
        player_count = 0
        bot.load_extension("services.ticket_system")
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name="DropTracker Bot", type=interactions.ActivityType.WATCHING))
    finally:
        local_session.close()




@slash_command(name="update-log", 
    description="Sends a formatted update log to a specific channel, optionally publishing it.",
               default_member_permissions=Permissions.ADMINISTRATOR)
@slash_option(name="input_str", description="Use | to split lines, +/- for additions/removals, ~ for changes", opt_type=OptionType.STRING, required=True)
@slash_option(name="version", description="The version number for this update.", opt_type=OptionType.STRING, required=False)
@slash_option(name="publish", description="If True, the message will be published in the announcement channel.", opt_type=OptionType.BOOLEAN, required=False)
@slash_option(name="should_ping", description="If True, will ping the updates role.", opt_type=OptionType.BOOLEAN, required=False)
async def update_log_message(ctx, input_str: str, version: str = None, publish: bool = False, should_ping: bool = False):
    """
    Sends a formatted update log to a specific channel, optionally publishing it.

    This function parses an input string with a simple syntax to create a rich,
    formatted Discord message.

    ## Input String Syntax:
    - Use `|` to separate each line or item.
    - Start a line with `+` for an addition (will appear green).
    - Start a line with `-` for a removal (will appear red).
    - Start a line with `~` for a change or fix (will appear in a neutral color).
    - Any other line will be treated as a general note.

    ## Parameters:
    - `bot`: Your `interactions.Client` instance.
    - `input_str`: A single string containing all update notes, formatted with the syntax above.
    - `version`: (Optional) The version number for this update (e.g., "v1.2.3").
    - `publish`: (Optional) If `True`, the message will be published in the announcement channel.

    ## Example Usage:
    `example_input = "+ Added a new `/profile` command | ~ Improved database query speed | - Removed the deprecated `/oldstats` command | This is a major performance update!"`
    `await update_log_message(bot, example_input, version="v2.5.0", publish=True)`
    """
    channel_id = channel_id_to_use  # Your #updates channel ID
    
    # --- 1. Parse the input string ---
    added, removed, changed, notes = [], [], [], []

    # Split the string by '|' and process each part
    items = [item.strip() for item in input_str.split('|')]
    for item in items:
        if not item:  # Skip empty parts resulting from extra '|'
            continue
        if item.startswith('+'):
            added.append(item[1:].strip())
        elif item.startswith('-'):
            removed.append(item[1:].strip())
        elif item.startswith('~'):
            changed.append(item[1:].strip())
        else:
            notes.append(item)

    # Abort if the input string was empty or contained no valid entries
    if not any([added, removed, changed, notes]):
        print("Input string was empty or invalid. No message sent.")
        await ctx.send("❌ Input string was empty or invalid. Please provide valid update content.", ephemeral=True)
        return

    # --- 2. Build the embedded message ---
    # Create the main embed with title and color
    embed_title = "🚀 UPDATE LOG 🚀"
    
    embed = Embed(
        title=embed_title,
        color=0x00ff00,  # Green color for updates
        timestamp=interactions.Timestamp.now()  # Current timestamp in footer
    )
    
    # Build the description with all sections
    description_parts = []

    # Build the "Added" section if items exist
    if added:
        description_parts.append("### ✅ **Added**")
        # Use a 'diff' code block for green prefixed lines
        diff_block = "```diff\n" + "\n".join(f"+ {item}" for item in added) + "```"
        description_parts.append(diff_block)

    # Build the "Removed" section if items exist
    if removed:
        description_parts.append("### ❌ **Removed**")
        # Use a 'diff' code block for red prefixed lines
        diff_block = "```diff\n" + "\n".join(f"- {item}" for item in removed) + "```"
        description_parts.append(diff_block)

    # Build the "Changes & Bug Fixes" section if items exist
    if changed:
        description_parts.append("### 🔧 **Changes / Bug Fixes**")
        # 'yaml' provides clean, neutral formatting for lists
        yaml_block = "```yaml\n" + "\n".join(f"- {item}" for item in changed) + "```"
        description_parts.append(yaml_block)

    # Build the "Notes" section for general text
    if notes:
        description_parts.append("### 📝 **Notes**")
        # Format notes as a simple bulleted list
        description_parts.append("\n".join(f"{note}" for note in notes))

    # Set the description
    embed.description = "\n".join(description_parts)
    
    # Set footer
    embed.set_footer(text=f"DropTracker - Update #{version}", icon_url="https://www.droptracker.io/img/droptracker-small.gif")
    
    
    # Prepare ping message if needed    
    ping_content = None
    if should_ping:
        ping_content = "<@&1279163761218949204>"
    
    # --- 3. Send the embedded message to the designated channel ---
    try:
        channel = await ctx.bot.fetch_channel(channel_id)


        # Send the embed with optional ping content
        message = await channel.send(content=ping_content, embeds=[embed])
        print(f"Update log successfully sent to #{channel.name}")
        
        # Send confirmation to the user
        await ctx.send(f"✅ Update log successfully sent to {channel.mention}!", ephemeral=True)

        # --- 4. Publish the message if requested and possible ---
        if publish and isinstance(channel, interactions.GuildNews):
            await message.publish()
            print(f"Message published to followed channels from announcement channel #{channel.name}")
            await ctx.edit_original_response(content=f"✅ Update log successfully sent to {channel.mention} and published!")

    except Exception as e:
        print(f"An error occurred while sending the update log: {e}")
        await ctx.send(f"❌ Error sending update log: {str(e)}", ephemeral=True)


@slash_command(name="update-add", 
               description="Add items to an update log draft",
               default_member_permissions=Permissions.ADMINISTRATOR)
@slash_option(name="version", description="Version number (e.g., 1.2.3)", opt_type=OptionType.STRING, required=True)
@slash_option(name="category", description="Category/App section (e.g., Discord Bot, Web App, API)", opt_type=OptionType.STRING, required=True)
@slash_option(name="type", description="Type of update item", opt_type=OptionType.STRING, required=True, choices=[
    interactions.SlashCommandChoice(name="Added", value="added"),
    interactions.SlashCommandChoice(name="Removed", value="removed"),
    interactions.SlashCommandChoice(name="Changed/Fixed", value="changed"),
    interactions.SlashCommandChoice(name="Note", value="notes")
])
@slash_option(name="content", description="The content to add", opt_type=OptionType.STRING, required=True)
async def update_add(ctx, version: str, category: str, type: str, content: str):
    """Add an item to an update log draft"""
    # Defer immediately to prevent timeout
    await ctx.defer(ephemeral=True)
    
    try:
        # Load existing data
        data = load_update_data(version)
        
        # Initialize category if it doesn't exist
        if category not in data["categories"]:
            data["categories"][category] = {
                "added": [],
                "removed": [],
                "changed": [],
                "notes": []
            }
        
        # Add the new content to the specified category
        if type in data["categories"][category]:
            data["categories"][category][type].append(content)
        else:
            await ctx.send(f"❌ Invalid type: {type}", ephemeral=True)
            return
        
        # Save the updated data
        save_update_data(version, data)
        
        # Send confirmation
        type_emoji = {"added": "✅", "removed": "❌", "changed": "🔧", "notes": "📝"}
        await ctx.send(
            f"{type_emoji.get(type, '📝')} Added to **{version}** → **{category}** {type}:\n```{content}```"
        )
        
    except Exception as e:
        await ctx.send(f"❌ Error adding to update log: {str(e)}")


@slash_command(name="update-view", 
               description="View a draft update log",
               default_member_permissions=Permissions.ADMINISTRATOR)
@slash_option(name="version", description="Version number to view", opt_type=OptionType.STRING, required=True)
async def update_view(ctx, version: str):
    """View a draft update log"""
    try:
        data = load_update_data(version)
        
        # Check if the update has any content
        has_content = False
        for category_data in data.get("categories", {}).values():
            if any([category_data.get("added"), category_data.get("removed"), 
                   category_data.get("changed"), category_data.get("notes")]):
                has_content = True
                break
        
        if not has_content:
            await ctx.send(f"📝 Update **{version}** is empty. Use `/update-add` to add content.", ephemeral=True)
            return
        
        # Create preview embed
        embed = Embed(
            title=f"🚀 UPDATE LOG DRAFT 🚀",
            color=0xffa500,  # Orange for draft
            description="**Preview of draft update log**"
        )
        
        # Add sections for each category
        for category, category_data in data.get("categories", {}).items():
            category_sections = []
            
            if category_data.get("added"):
                added_text = "\n".join(f"+ {item}" for item in category_data["added"])
                category_sections.append(f"**✅ Added**\n```diff\n{added_text}```")
            
            if category_data.get("removed"):
                removed_text = "\n".join(f"- {item}" for item in category_data["removed"])
                category_sections.append(f"**❌ Removed**\n```diff\n{removed_text}```")
            
            if category_data.get("changed"):
                changed_text = "\n".join(f"- {item}" for item in category_data["changed"])
                category_sections.append(f"**🔧 Changes/Fixes**\n```yaml\n{changed_text}```")
            
            if category_data.get("notes"):
                notes_text = "\n".join(f"{note}" for note in category_data["notes"])
                category_sections.append(f"**📝 Notes**\n{notes_text}")
            
            if category_sections:
                # Combine all sections for this category
                category_content = "\n\n".join(category_sections)
                # Truncate if too long for embed field
                if len(category_content) > 1024:
                    category_content = category_content[:1021] + "..."
                embed.add_field(name=f"📂 {category}", value=category_content, inline=False)
        
        # Add metadata
        created_at = datetime.fromisoformat(data.get("created_at", datetime.now().isoformat()))
        modified_at = datetime.fromisoformat(data.get("last_modified", datetime.now().isoformat()))
        
        embed.set_footer(text=f"Created: {created_at.strftime('%Y-%m-%d %H:%M')} | Modified: {modified_at.strftime('%Y-%m-%d %H:%M')}")
        
        await ctx.send(embeds=[embed], ephemeral=True)
        
    except Exception as e:
        await ctx.send(f"❌ Error viewing update log: {str(e)}", ephemeral=True)


@slash_command(name="update-list", 
               description="List all available update log drafts",
               default_member_permissions=Permissions.ADMINISTRATOR)
async def update_list(ctx):
    """List all available update log drafts"""
    try:
        versions = list_update_versions()
        
        if not versions:
            await ctx.send("📝 No update log drafts found. Use `/update-add` to create one.", ephemeral=True)
            return
        
        # Create list embed
        embed = Embed(
            title="📋 Update Log Drafts",
            color=0x3498db,  # Blue
            description=f"Found **{len(versions)}** draft(s)"
        )
        
        # Add version list
        version_list = []
        for version in versions[:10]:  # Limit to 10 most recent
            data = load_update_data(version)
            # Count items across all categories
            item_count = 0
            for category_data in data.get("categories", {}).values():
                item_count += sum(len(category_data.get(key, [])) for key in ["added", "removed", "changed", "notes"])
            
            category_count = len(data.get("categories", {}))
            modified = datetime.fromisoformat(data.get("last_modified", datetime.now().isoformat()))
            version_list.append(f"**{version}** - {item_count} items in {category_count} categories - {modified.strftime('%m/%d %H:%M')}")
        
        embed.add_field(
            name="Recent Versions", 
            value="\n".join(version_list) if version_list else "None",
            inline=False
        )
        
        if len(versions) > 10:
            embed.add_field(name="Note", value=f"Showing 10 of {len(versions)} versions", inline=False)
        
        await ctx.send(embeds=[embed], ephemeral=True)
        
    except Exception as e:
        await ctx.send(f"❌ Error listing update logs: {str(e)}", ephemeral=True)


@slash_command(name="update-send", 
               description="Send a completed update log from draft",
               default_member_permissions=Permissions.ADMINISTRATOR)
@slash_option(name="version", description="Version number to send", opt_type=OptionType.STRING, required=True)
@slash_option(name="publish", description="Publish the message if in announcement channel", opt_type=OptionType.BOOLEAN, required=False)
@slash_option(name="should_ping", description="Ping the updates role", opt_type=OptionType.BOOLEAN, required=False)
async def update_send(ctx, version: str, publish: bool = False, should_ping: bool = False):
    """Send a completed update log from draft"""
    try:
        data = load_update_data(version)
        
        # Check if the update has any content
        has_content = False
        for category_data in data.get("categories", {}).values():
            if any([category_data.get("added"), category_data.get("removed"), 
                   category_data.get("changed"), category_data.get("notes")]):
                has_content = True
                break
        
        if not has_content:
            await ctx.send(f"❌ Update **{version}** is empty. Use `/update-add` to add content first.", ephemeral=True)
            return
        
        channel_id = channel_id_to_use  # Your #updates channel ID
        
        # Build the embedded message
        embed_title = f"🚀 UPDATE LOG 🚀"
        
        embed = Embed(
            title=embed_title,
            color=0x00ff00,  # Green color for updates
            timestamp=interactions.Timestamp.now()
        )
        
        # Build the description with all categories and sections
        description_parts = []

        # Build sections for each category
        for category, category_data in data.get("categories", {}).items():
            category_sections = []
            
            # Add category header (larger)
            if category != "Summary":
                description_parts.append(f"### 📂 **{category}**")
            
            if category_data.get("added"):
                description_parts.append("✅ **Added**")
                diff_block = "```diff\n" + "\n".join(f"+ {item}" for item in category_data["added"]) + "```"
                description_parts.append(diff_block)

            if category_data.get("removed"):
                description_parts.append("❌ **Removed**")
                diff_block = "```diff\n" + "\n".join(f"- {item}" for item in category_data["removed"]) + "```"
                description_parts.append(diff_block)

            if category_data.get("changed"):
                description_parts.append("🔧 **Changes/Fixes**")
                yaml_block = "```yaml\n" + "\n".join(f"- {item}" for item in category_data["changed"]) + "```"
                description_parts.append(yaml_block)

            if category_data.get("notes"):
                description_parts.append("📝 **Notes**")
                description_parts.append("\n".join(f"{note}" for note in category_data["notes"]))
            
            # Add spacing between categories
            description_parts.append("")

        # Set the description
        embed.description = "\n".join(description_parts).strip()
        
        # Set footer
        embed.set_footer(text=f"DropTracker - Update #{version}", icon_url="https://www.droptracker.io/img/droptracker-small.gif")
        
        # Prepare ping message if needed    
        ping_content = None
        if should_ping:
            ping_content = "<@&1279163761218949204>"
        
        # Send the embedded message to the designated channel
        channel = await ctx.bot.fetch_channel(channel_id)


        # Send the embed with optional ping content
        message = await channel.send(content=ping_content, embeds=[embed])
        print(f"Update log successfully sent to #{channel.name}")
        
        # Send confirmation to the user
        await ctx.send(f"✅ Update log **{version}** successfully sent to {channel.mention}!", ephemeral=True)
        if publish:
            ## Attempt to send the update as a DM to all users with DMs enabled
            local_session = Session()
            user_discord_ids = local_session.query(User.discord_id).all()
            ## Store a list of these IDs in a file
            os.makedirs("data/updates/to_send", exist_ok=True)
            with open(f"data/updates/to_send/{version}.txt", "w") as f: 
                print(f"{len(user_discord_ids)} users to send to")
                for user_discord_id in user_discord_ids:
                    should_dm = local_session.query(UserConfiguration).filter(UserConfiguration.user_id == user_discord_id[0], UserConfiguration.config_key == "dm_on_update_logs").first()
                    if should_dm and should_dm.config_value == "true":
                        user_discord_id = user_discord_id[0]
                        f.write(str(user_discord_id) + ",")

            

        # Publish the message if requested and possible
        if publish and isinstance(channel, interactions.GuildNews):
            await message.publish()
            print(f"Message published to followed channels from announcement channel #{channel.name}")
        
    except Exception as e:
        await ctx.send(f"❌ Error sending update log: {str(e)}", ephemeral=True)


@slash_command(name="update-delete", 
               description="Delete an update log draft",
               default_member_permissions=Permissions.ADMINISTRATOR)
@slash_option(name="version", description="Version number to delete", opt_type=OptionType.STRING, required=True)
async def update_delete(ctx, version: str):
    """Delete an update log draft"""
    try:
        file_path = get_update_file_path(version)
        
        if not os.path.exists(file_path):
            await ctx.send(f"❌ Update log **{version}** not found.", ephemeral=True)
            return
        
        # Delete the file
        os.remove(file_path)
        
        await ctx.send(f"🗑️ Update log **{version}** has been deleted.", ephemeral=True)
        
    except Exception as e:
        await ctx.send(f"❌ Error deleting update log: {str(e)}", ephemeral=True)


if __name__ == "__main__":
    bot.start()