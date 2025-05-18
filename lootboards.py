from services.lootboards import generate_server_board
from db.models import Group, Session
import asyncio
import os

session = None

async def lootboard_update_loop():
    print("Starting lootboard update loop")
    try:
        await update_boards()
    except Exception as e:
        print(f"Exception in lootboard_update_loop: {e}")
    # Wait 2 minutes before the next iteration
    return True

def get_fresh_session():
    global session
    if session:
        session.reset()
        session.rollback()
        session.close()
    session = Session()
    return session

async def update_boards():
    try:
        # Get all groups with a dedicated session that's immediately closed
        original_groups = []
        with Session() as temp_session:
            try:
                original_groups = temp_session.query(Group).all()
            except Exception as e:
                temp_session.rollback()
                try:
                    original_groups = temp_session.query(Group).all()
                except Exception as e:
                    print(f"Error getting groups: {e}")
                    return
            
        # Create a clean list of groups outside the session
        groups = []
        for g in original_groups:
            if g.guild_id and g.guild_id != 0:
                temp_group = Group(group_name=g.group_name, guild_id=g.guild_id, wom_id=g.wom_id)
                temp_group.group_id = g.group_id
                groups.append(temp_group)
                
        # Process each group independently with its own session
        print(f"Found {len(groups)} groups to process")
        for group in groups:
            try:
                if not os.path.exists(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb"):
                    os.makedirs(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb")
                
                # Create a completely new session for each group
                with Session() as group_session:
                    try:
                        new_path = await generate_server_board(group_id=group.group_id, wom_group_id=group.wom_id, session_to_use=group_session)
                        print(f"Board generated for {group.group_name}")
                        print(f"Board path: {new_path}")
                    except Exception as e:
                        print(f"Error generating board for group {group.group_id}: {e}")
                        # No need to explicitly rollback - the context manager will handle it
            except Exception as e:
                print(f"Error in group processing for {group.group_id}: {e}")
                continue
        
    except Exception as e:
        print(f"Error updating boards: {e}")
    finally:
        print("Finished cycle and closed sessions.")
    
    print("Completed lootboard update loop. Waiting 2 minutes to continue")

async def startup():
    print("Starting lootboard update loop")
    await lootboard_update_loop()

if __name__ == "__main__":
    asyncio.run(startup())
    exit()
