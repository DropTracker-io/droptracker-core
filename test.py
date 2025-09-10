import asyncio
import os
import interactions
from dotenv import load_dotenv
from interactions import listen
from interactions.api.events import Startup
from sqlalchemy import text
from db.models import (
    User, session, Guild, Group, GroupConfiguration,
    GroupEmbed, GroupNotification, NotifiedSubmission,
    GroupRecentDrops, GroupPersonalBestMessage,
    NotificationQueue, GroupPatreon, GroupWomAssociation, LBUpdate
)
from db.models import Player
from services.points import award_points_to_player
from utils.messages import create_points_embed

load_dotenv()

bot = interactions.Client()

# Dry-run controls: Only allow side effects for specific test cases
ALLOWED_TEST_DISCORD_ID = "528746710042804247"

def _can_perform_side_effects(first_player: Player, user: User) -> bool:
    try:
        if first_player and getattr(first_player, "user_id", None) == 0:
            return True
        if user and str(getattr(user, "discord_id", "")) == ALLOWED_TEST_DISCORD_ID:
            return True
    except Exception:
        pass
    return False

@listen(Startup)
async def on_ready(event):
    # Use a direct SQL query since we're interacting with a separate database
    try:
        result = session.execute(
            text("SELECT external_id FROM xenforo.xf_user WHERE external_id IS NOT NULL")
        )
        users_to_dm_registrations = []
        for row in result:
            first_player = session.query(Player).filter(Player.user_id == row[0]).first()
            if first_player:
                user = session.query(User).filter(User.user_id == first_player.user_id).first()
                if _can_perform_side_effects(first_player, user):
                    award_points_to_player(player_id=first_player.player_id, amount=50, source="Account Registration (website)", expires_in_days=30)
                    if user and user.discord_id:
                        users_to_dm_registrations.append(user.discord_id)
                else:
                    print(f"DRY_RUN: Skipping registration award/DM for user_id={first_player.user_id}, discord_id={(user.discord_id if user else 'N/A')}")
    except Exception as e:
        print(f"Error querying external users: {e}")
    # Next, award all users for their first registered RSNs
    result = session.execute(
        text("SELECT player_id FROM players WHERE (user_id, player_id) IN (SELECT user_id, MIN(player_id) FROM players WHERE user_id IS NOT NULL GROUP BY user_id)")
    )
    users_to_dm_rsns = []
    for row in result:
        first_player = session.query(Player).filter(Player.player_id == row[0]).first()
        if first_player:
            user = session.query(User).filter(User.user_id == first_player.user_id).first()
            if _can_perform_side_effects(first_player, user):
                award_points_to_player(player_id=first_player.player_id, amount=10, source="RSN claimed (/claim-rsn)", expires_in_days=30)
                if user and user.discord_id:
                    users_to_dm_rsns.append(user.discord_id)
            else:
                print(f"DRY_RUN: Skipping RSN award/DM for user_id={first_player.user_id}, discord_id={(user.discord_id if user else 'N/A')}")
    users_to_dm_both = [user_id for user_id in users_to_dm_registrations if user_id in users_to_dm_rsns]
    for user_id in users_to_dm_registrations:
        if user_id not in users_to_dm_both:
            if str(user_id) == ALLOWED_TEST_DISCORD_ID:
                user = await bot.fetch_user(user_id)
                embed = create_points_embed(points=50, source="Account Registration (website)", expires_in_days=60, player=first_player, user=user)
                await user.send(content=f"Hey, <@{user_id}>!\n", embeds=[embed])
            else:
                print(f"DRY_RUN: Skipping DM (registration) to discord_id={user_id}")
    for user_id in users_to_dm_rsns:
        if user_id not in users_to_dm_both:
            if str(user_id) == ALLOWED_TEST_DISCORD_ID:
                user = await bot.fetch_user(user_id)
                embed = create_points_embed(points=10, source="RSN claimed (/claim-rsn)", expires_in_days=60, player=first_player, user=user)
                await user.send(content=f"Hey, <@{user_id}>!\n", embeds=[embed])
            else:
                print(f"DRY_RUN: Skipping DM (RSN) to discord_id={user_id}")
    for user_id in users_to_dm_both:
        if str(user_id) == ALLOWED_TEST_DISCORD_ID:
            user = await bot.fetch_user(user_id)
            embed_registration = create_points_embed(points=50, source="Account Registration (website)", expires_in_days=60, player=first_player, user=user)
            embed_rsn = create_points_embed(points=10, source="RSN claimed (/claim-rsn)", expires_in_days=60, player=first_player, user=user)
            await user.send(content=f"Hey, <@{user_id}>!\n", embeds=[embed_registration, embed_rsn])
        else:
            print(f"DRY_RUN: Skipping DM (both) to discord_id={user_id}")
        await asyncio.sleep(0.1)
if __name__ == "__main__":
    print("Starting bot")
    bot.start(token=os.getenv("BOT_TOKEN"))