import requests
import asyncio
import os
from dotenv import load_dotenv
# Assuming User and session are correctly defined in your db.models
from db.models import Group, User, session, GroupPatreon
from sqlalchemy import func
from utils.messages import new_patreon_sub
from utils.logger import LoggerClient
load_dotenv()
logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))

from interactions import Task, IntervalTrigger

@Task.create(IntervalTrigger(minutes=60))
async def patreon_sync():
    await logger.log("access", "Patreon sync task started...", "patreon_sync")
    new, updated = await get_creator_patreon_data()
    if new:
        for member in new:
            user = member['user']
            group_id = member['group_id']
            tier = member['tier']
            group = None
            if group_id:
                group = session.query(Group).filter(Group.group_id == group_id).first()
            try:
                print("TODO -- send a patreon message!!!")
                #await new_patreon_sub(bot, user_id=user, sub_type=tier, group=group)
                #await asyncio.sleep(5)
                ## sleep incase there are multiple subs in the same update
            except Exception as e:
                print("Couldn't send patreon sub msg:", e)


async def get_creator_patreon_data():
    url = "https://www.patreon.com/api/oauth2/v2/campaigns/12053510/members"
    headers = {
        "Authorization": f"Bearer {os.getenv('PATREON_ACCESS_TOKEN')}"
    }
    params = {
        "include": "currently_entitled_tiers,user",  # Include user info
        "fields[member]": "patron_status,pledge_relationship_start,full_name,email",
        "fields[user]": "social_connections"
    }
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        patreon_members = parse_patreon_members(data)
        print("Got Patreon response, updating database.")
        return await update_database(patreon_members)
    else:
        print(f"Failed to fetch data. Status code: {response.status_code}")
        return None

def parse_patreon_members(data):
    patreon_members = []
    included_users = {user['id']: user for user in data.get('included', [])}

    for member in data.get('data', []):
        attributes = member.get('attributes', {})
        relationships = member.get('relationships', {})
        
        full_name = attributes.get('full_name', 'Unknown')
        email = attributes.get('email', 'Unknown')
        patron_status = attributes.get('patron_status', 'None')
        discord_id = None
        currently_entitled_tiers = relationships.get('currently_entitled_tiers', [])
        tier = 0
        if currently_entitled_tiers:
            tiers = currently_entitled_tiers['data']
            if tiers:
                for pt_tier in tiers:
                    if int(pt_tier['id']) == 22736754:
                        tier = 1
                    elif int(pt_tier['id']) == 22736762:
                        tier = 2
                    elif int(pt_tier['id']) == 22736787:
                        tier = 3
                    elif int(pt_tier['id']) == 23798233:
                        tier = 4
                    elif int(pt_tier['id']) == 23798236:
                        tier = 5
                    else:
                        continue

        user_relationship = relationships.get('user', {}).get('data', {})
        user_id = user_relationship.get('id')
        if user_id and user_id in included_users:
            social_connections = included_users[user_id].get('attributes', {}).get('social_connections', {})
            if social_connections.get('discord', None):
                discord_id = social_connections.get('discord', {}).get('user_id')
        patreon_members.append({
            'full_name': full_name,
            'email': email,
            'patron_status': patron_status,
            'discord_id': discord_id,
            'tier': tier
        })
    
    return patreon_members


async def update_database(patreon_members):
    new_subscriptions = []  # List to track new subscriptions
    updated_subscriptions = []  # List to track updated subscriptions

    for member in patreon_members:
        user: User = session.query(User).filter(User.discord_id == member['discord_id']).first()
        if user:
            if member['discord_id'] and member['patron_status'] == "active_patron":
                try:
                    existing_entry = session.query(GroupPatreon).filter_by(user_id=user.user_id).first()
                    if member['tier'] >= 1:  
                        if existing_entry:
                            # Update existing entry
                            existing_entry.patreon_tier = member['tier']
                            existing_entry.date_updated = func.now()
                            if existing_entry.group_id == None:
                                if user.groups:
                                    existing_entry.group_id = user.groups[0].group_id
                                    session.commit()
                            updated_subscriptions.append({
                                'user': user,
                                'tier': member['tier'],
                                'group_id': existing_entry.group_id
                            })
                        else:
                            if user.groups:
                                # Create new GroupPatreon entry
                                new_group_patreon = GroupPatreon(
                                    user_id=user.user_id,
                                    group_id=user.groups[0].group_id, 
                                    patreon_tier=member['tier'],
                                    date_added=func.now()
                                )
                            else:
                                new_group_patreon = GroupPatreon(
                                    user_id=user.user_id,
                                    group_id=None, 
                                    patreon_tier=member['tier'],
                                    date_added=func.now()
                                )
                            session.add(new_group_patreon)
                            session.commit()
                            
                            new_subscriptions.append({
                                'user': user,
                                'tier': member['tier'],
                                'group_id': new_group_patreon.group_id
                            })
                        session.commit()
                except Exception as e:
                    print(f"Couldn't update Patreon info for {user.username}: {e}")
        else:
            pass  # Discord account not linked
    return new_subscriptions, updated_subscriptions
