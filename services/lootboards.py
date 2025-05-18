"""
    Handles the process of updating loot leaderboard embeds in Discord based on group configurations

"""
import os
import interactions
from interactions import Extension, Task, IntervalTrigger
from db.models import Group, GroupConfiguration, session
from datetime import datetime, timedelta
import time
import asyncio
from db.app_logger import AppLogger
from db.models import Session, session, User, Player
from utils.wiseoldman import fetch_group_members
from utils.format import replace_placeholders
from lootboard.generator import generate_server_board
from db.ops import DatabaseOperations, associate_player_ids

db = DatabaseOperations()
class Lootboards(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        print(f"Loot leaderboard service initialized.")
    
    

