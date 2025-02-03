import os
import asyncio
import httpx
from asynciolimiter import Limiter
from dotenv import load_dotenv
from db.models import Player, session
import wom
from wom import Err
load_dotenv()

rate_limit = 100 / 65  # This calculates the rate as 100 requests per 65 seconds
limiter = Limiter(rate_limit)  # Create a Limiter instance

# Fetch the WOM_API_KEY from environment variables
WOM_API_KEY = os.getenv("WOM_API_KEY")

# Initialize the WOM Client with API key and user agent
client = wom.Client(
    WOM_API_KEY,
    user_agent="@joelhalen"
)

async def check_user_by_username(username: str):
    """ Check a user in the WiseOldMan database, returning their "player" object,
        their WOM ID, and their displayName.
    """
    # TODO -- only grab necessary info and parse it before returning the full player obj?
    await limiter.wait()
    await client.start()  # Initialize the client (if required by the `wom` library)

    try:
        result = await client.players.get_details(username=username)
        # Add debug logging
        try:
            if result.is_ok:
                player = result.unwrap()
                if player is None:
                    return None, None, None
                return player, player.username, player.id
        except Exception as e:
            error = result.unwrap_err()
            if isinstance(error, Err):
                pass
        else:
            error = result.unwrap_err()
            if isinstance(error, Err):
                pass
            # Try update if get fails
            try:
                result = await client.players.update_player(username=username)
            except Exception as e:
                print("Error updating player:", e)
            # Add debug logging
            if not result.is_ok:
                print(f"Update player failed for {username}. Status: {result.status_code}")
                
            if result.is_ok:
                player = result.unwrap()
                
                if player is None:
                    print(f"Got empty player object after update for {username}")
                    return None, None, None
                print("Got player object after update for", username + ":", player)
                player_id = player.id
                player_name = player.username
                return player, str(player_name), str(player_id)
            else:
                print("Result is not ok, returning None")
                return None, None, None
    except Exception as e:
        print(f"Error checking user {username}: {str(e)}")
        return None, None, None

async def check_user_by_id(uid: int):
    """ Check a user in the WiseOldMan database, returning their "player" object,
        their WOM ID, and their displayName.
    """
    await client.start()  # Initialize the client (if required by the `wom` library)

    await limiter.wait()

    try:
        result = await client.players.get_details(id=uid)
        if result.is_ok:
            player = result.unwrap()
            player_id = player.player.id
            player_name = player.player.display_name
            return player, str(player_name), str(player_id)
        else:
            # Handle the case where the request failed
            return None, None, None
    finally:
        pass

async def check_group_by_id(wom_group_id: int):
    """ Searches for a group on WiseOldMan by a passed group ID 
        Returns group_name, member_count and members (list)    
    """
    wom_id = str(wom_group_id)
    await client.start()
    await limiter.wait()
    try:
        result = await client.groups.get_details(id=wom_id)
        if result.is_ok:
            details = result.unwrap()
            members = details.memberships
            member_count = details.group.member_count
            group_name = details.group.name
            return group_name, member_count, members
        else:
            return None, None, None
    finally:
        pass

async def fetch_group_members(wom_group_id: int):
    """ 
    Returns a list of WiseOldMan Player IDs 
    for members of a specified group 
    """
    #print("Fetching group members for ID:", wom_group_id)
    user_list = []
    
    if wom_group_id == 1:
        # Fetch all player WOM IDs from the database directly
        players = session.query(Player.wom_id).all()
        # Unpack the list of tuples returned by SQLAlchemy
        user_list = [player.wom_id for player in players] 
        return user_list
    await client.start()
    await limiter.wait()
    try:
        result = await client.groups.get_details(wom_group_id)
        if result.is_ok:
            details = result.unwrap()
            members = details.memberships
            for member in members:
                user_list.append(member.player_id)
            return user_list
        else:
            return []
    except Exception as e:
        print("Couldn't find WOM group members... Error:", e)
        return []
