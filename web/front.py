# api.py

from datetime import datetime, timedelta
import json
import os
import re
import shutil
from types import TracebackType
import interactions
import markdown
from utils.format import get_sorted_doc_files, convert_from_ms, parse_authed_users, human_readable_time_difference
from db.models import session, NpcList

from quart import Blueprint, jsonify, redirect, render_template, request, session as sesh, send_from_directory, url_for
from quart_jwt_extended import (
    JWTManager,
    jwt_required,
    create_access_token,
    get_jwt_identity,
    decode_token
)
from db.ops import DatabaseOperations
from db.models import CollectionLogEntry, CombatAchievementEntry, Drop, Group, GroupConfiguration, GroupPatreon, ItemList, NotifiedSubmission, NpcList, PersonalBestEntry, Player, UserConfiguration, session as db_sesh, User, Guild

missing_file_log_path = "missing_images.json"

DOCS_FOLDER = os.path.join(os.getcwd(), 'templates/docs')

def create_frontend(bot: interactions.Client):
# Create a Blueprint object
    front = Blueprint('frontend', __name__)

    db = DatabaseOperations()
    # Define path to docs folder

    @front.route('/')
    async def homepage():
        
        user = sesh.get('user', None)
        print("Session data:", dict(sesh))
        print("User:", user)
        jwt_token = sesh.get('jwt_token', None)
        print("JWT Token:", jwt_token)
        if not user:
            return await render_template('index.html',
                                     page_name="Home",
                                     current_page="home")
        else:
            return await render_template("index.html", 
                                     user=user,
                                     page_name="Home",
                                     current_page="home")
        
    @front.route('/img/<path:filename>')
    async def serve_img(filename):
        ## Check if the file exists
        if not os.path.exists(os.path.join('static/assets/img', filename)):
            if ".png" in filename or ".jpeg" in filename or ".jpg" in filename or ".gif" in filename:
                target = filename.replace(".png", "")
                target = filename.replace(".jpeg", "")
                target = filename.replace(".jpg", "")
                target = filename.replace(".gif", "")
                target = filename.replace("npcdb/", "")
                npc = session.query(NpcList).filter(NpcList.npc_id == target).first()
                ## Add the file to the missing file log
                ## Check if the file exists in the backup path
                if npc:
                    npc_name = npc.npc_name
                    formatted_name = npc_name.replace(" ", "_").replace("'", "").replace("(","").replace(")","")
                    formatted_name = formatted_name.lower() + ".png"
                    backup_path = os.path.join('static/assets/img/npc_backup/', formatted_name)
                    if os.path.exists(backup_path):
                        ## Copy the file to the main path
                        shutil.copy(backup_path, os.path.join('static/assets/img', filename))
                        return await send_from_directory('static/assets/img/npc_backup/', formatted_name)
                    else:
                        print(f"Image not found: {filename}")
                        with open(missing_file_log_path, 'a') as f:
                            f.write(f"{filename} - {npc_name}\n")
            else:
                print(f"No .png, .jpeg, or .jpg extension found in {filename}")
            return await send_from_directory('static/assets/img', 'droptracker-small.gif')
        return await send_from_directory('static/assets/img', filename)
    
    @front.route('/user-upload/<path:filename>')
    async def serve_user_img(filename):
        return await send_from_directory('static/assets/img/user-upload')
  
    return front

async def get_guild(bot: interactions.Client, guild_id):
    print("get_guild called with bot:", bot, "and guild_id:", guild_id)
    print(f"Bot:", bot.user.username, "ID", bot.user.id)
    try:
        guild = await bot.fetch_guild(guild_id=guild_id)
        return guild
    except Exception as e:
        print("Couldn't get the guild with .fetch_guild:", e)
    return None
