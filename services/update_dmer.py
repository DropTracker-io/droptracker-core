import interactions
import asyncio
from interactions.api.events import Startup
from interactions import Extension, IntervalTrigger, Task, listen, Embed
import os
from webhook_bot import load_update_data
from db.models import Session, UserConfiguration, session

class UpdateDirectMessager(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        #self.check_update_dms.start()

    @Task.create(IntervalTrigger(seconds=5))
    async def check_update_dms(self):
        print("Checking for update DMs")
        dir = "/store/droptracker/disc/data/updates/to_send"
        for file in os.listdir(dir):
            version = file.replace(".txt", "")
            data = load_update_data(version)
        
        # Check if the update has any content
        has_content = False
        for category_data in data.get("categories", {}).values():
            if any([category_data.get("added"), category_data.get("removed"), 
                   category_data.get("changed"), category_data.get("notes")]):
                has_content = True
                break
        
        if not has_content:
            print(f"❌ Update **{version}** is empty. Use `/update-add` to add content first.")
            return
        
        # Build the embedded message
        embed_title = f"🚀 UPDATE LOG 🚀"
        
        embed = interactions.Embed(
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
        try:
            local_session = Session()
            for file in os.listdir(dir):
                with open(os.path.join(dir, file), "r") as f:
                    user_ids = f.read().split(",")
                added_ids = []
                for user_id in user_ids:
                    if user_id is None or user_id == '':
                        continue
                    if user_id in added_ids:
                        continue
                    should_dm = local_session.query(UserConfiguration).filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == "dm_on_update_logs").first()
                    
                    # If config doesn't exist, create it with default value (true)
                    if not should_dm:
                        should_dm = UserConfiguration(
                            user_id=user_id,
                            config_key="dm_on_update_logs",
                            config_value="true"  # Default to enabled
                        )
                        local_session.add(should_dm)
                        session.commit()
                        print(f"Created dm_on_update_logs config for user {user_id} with default value 'true'")
                    
                    if should_dm and should_dm.config_value == "true":
                        try:
                            added_ids.append(user_id)
                            print(f"Sending update to {user_id}")
                            user = await self.bot.fetch_user(user_id)
                            
                            await user.send(content=f"Hey, <@{user_id}>!\n" + 
                            "**As this is our first direct message to all registered users, we ask that you please join our [discord server](https://discord.gg/dvb7yP7JJH) to stay in-the-loop!**\n" + 
                            "-# You are receiving this message because you have `direct-message updates enabled` in the DropTracker.\n" + 
                            "-# You can change this setting at any time using the </dm-settings:1413653507705405524> command, or through your [account settings page](https://www.droptracker.io/account/droptracker).", embeds=[embed])
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            continue ## permissions or privacy issues
                with open(os.path.join(dir, file), "w") as f:
                    f.write("")  # wipes the list after sending
        except Exception as e:
            print(f"Error sending update to users: {e}")
            ## Attempt to rollback incase this solves any database issues that may have occurred
            local_session.rollback()
            raise
        finally:
            local_session.close()