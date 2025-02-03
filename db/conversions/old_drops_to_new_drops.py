import sys
import os
import re
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from db.models import Player, Drop, NpcList, ItemList  # Import the necessary models

# Step 1: Read and Parse the SQL File
sql_file_path = '/store/tempdb/drops.sql'
ignored_npcs_file = 'ignored_npcs.txt'
last_processed_id_file = 'last_processed_id.txt'

# Persistent ignored NPC list
def load_ignored_npcs():
    if os.path.exists(ignored_npcs_file):
        with open(ignored_npcs_file, 'r') as file:
            return set(file.read().splitlines())
    return set()

def save_ignored_npcs(ignored_npcs):
    with open(ignored_npcs_file, 'w') as file:
        file.write("\n".join(ignored_npcs))

# Persistent last processed drop ID
def load_last_processed_id():
    if os.path.exists(last_processed_id_file):
        with open(last_processed_id_file, 'r') as file:
            return int(file.read().strip())
    return 0

def save_last_processed_id(drop_id):
    with open(last_processed_id_file, 'w') as file:
        file.write(str(drop_id))

# Load ignored NPCs and last processed drop ID
ignored_npcs = load_ignored_npcs()
last_processed_drop_id = load_last_processed_id()
processed_entries = last_processed_drop_id

# Define DropObject class
class DropObject:
    def __init__(self, drop_id: int, item_name: str, item_id: int, rsn: str, quantity: int, value: int, time_str: str,
                 notified: int, image_url: str, npc_name: str, ym_partition: int) -> None:
        self.drop_id = int(drop_id)  # Ensure drop_id is cast to an integer
        self.item_name = item_name
        self.item_id = int(item_id)  # Ensure item_id is cast to an integer
        self.rsn = rsn
        self.quantity = int(quantity)  # Ensure quantity is an integer
        self.value = int(value)  # Ensure value is an integer
        self.time_str = time_str
        self.notified = int(notified)  # Ensure notified is an integer
        self.image_url = image_url
        self.npc_name = npc_name
        self.ym_partition = int(ym_partition)  # Ensure ym_partition is an integer

# Read and parse SQL file
with open(sql_file_path, 'r') as file:
    sql_content = file.read()

insert_data_pattern = re.compile(r"INSERT INTO `drops` \(.*?\) VALUES\s*(.+?);", re.DOTALL)
matches = insert_data_pattern.findall(sql_content)

total_entries = sum([len(re.findall(r"\((.*?)\)", match, re.DOTALL)) for match in matches])

print(f"Total entries to process: {total_entries}")
total_entries = total_entries - processed_entries

drops_data = []
for match in matches:
    data_tuples = re.findall(r"\((.*?)\)", match, re.DOTALL)
    for data_tuple in data_tuples:
        values = [v.strip().strip("'") for v in data_tuple.split(",")]
        if len(values) == 11:  # Ensure there are 11 fields
            drop_id, item_name, item_id, rsn, quantity, value, time_str, notified, image_url, npc_name, ym_partition = values
            if int(drop_id) > last_processed_drop_id:  # Skip drops we've already processed
                new_drop = DropObject(drop_id, item_name, item_id, rsn, quantity, value, time_str, notified, image_url, npc_name, ym_partition)
                drops_data.append(new_drop)

DB_USER = os.getenv('DB_USER')
DB_PASS = os.getenv('DB_PASS')
engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data')  
Session = sessionmaker(bind=engine)
session = Session()

player_cache = {}
npc_ids = {}
item_ids = {}

players = session.query(Player).all()
for player in players:
    player_cache[player.player_name.lower()] = player.player_id

# Load existing items into a cache
items = session.query(ItemList).all()
for item in items:
    item_ids[item.item_id] = item

# Function to check if an NPC name has disallowed characters
def has_disallowed_characters(npc_name):
    return any(char in npc_name for char in ["(", "'", "_"])  # Add more if necessary

# Track progress
processed_entries = 0
final_list = []

for drop_object in drops_data:
    drop_object: DropObject = drop_object
    rsn = drop_object.rsn
    drop_id = drop_object.drop_id
    npc_name = drop_object.npc_name
    player_id = player_cache.get(rsn.lower())

    if not player_id:
        player = session.query(Player).filter(func.lower(Player.player_name) == rsn.lower()).first()
        if player:
            player_id = player.player_id
            player_cache[rsn.lower()] = player_id
        else:
            continue

    normalized_name = npc_name.replace("\\'", "'")

    if normalized_name in ignored_npcs:
        continue

    # NPC Handling
    if normalized_name not in npc_ids:
        npc = session.query(NpcList).filter(NpcList.npc_name == normalized_name).first()
        if not npc:
            if not has_disallowed_characters(normalized_name):
                print(f"\rNPC '{normalized_name}' not found for drop {drop_id}. It has been ignored.")
                ignored_npcs.add(normalized_name)
                save_ignored_npcs(ignored_npcs)
                continue
            else:
                ignored_npcs.add(normalized_name)
                save_ignored_npcs(ignored_npcs)
                continue
        else:
            npc_ids[normalized_name] = npc.npc_id

    # Item Handling
    if drop_object.item_id not in item_ids:
        print(f"Item '{drop_object.item_name}' (ID: {drop_object.item_id}) not found for drop {drop_id}.")
        user_input = input("Do you want to add this item? (y/n): ").strip().lower()
        if user_input == 'y':
            new_item = ItemList(item_id=drop_object.item_id, item_name=drop_object.item_name, noted=False)
            session.add(new_item)
            try:
                session.commit()
                item_ids[drop_object.item_id] = new_item
                print(f"Item '{drop_object.item_name}' added to the database.")
            except Exception as e:
                session.rollback()
                print(f"Error adding item '{drop_object.item_name}': {e}")
                continue
        else:
            print(f"Item '{drop_object.item_name}' has been skipped.")
            continue

    new_drop = Drop(
        drop_id=drop_object.drop_id,
        item_id=drop_object.item_id,
        player_id=player_id,
        quantity=drop_object.quantity,
        value=drop_object.value,
        date_added=datetime.strptime(drop_object.time_str, '%Y-%m-%d %H:%M:%S'),
        npc_id=npc_ids[normalized_name],
        image_url=drop_object.image_url,
        partition=drop_object.ym_partition
    )

    final_list.append(new_drop)

    processed_entries += 1
    progress_percentage = (processed_entries / total_entries) * 100
    sys.stdout.write(f"\rProgress: {processed_entries}/{total_entries} ({progress_percentage:.2f}% complete)")
    sys.stdout.flush()

    if processed_entries % 10000 == 0:
        session.add_all(final_list)
        session.commit()
        final_list = []
        save_last_processed_id(drop_id)

try:
    session.add_all(final_list)
    session.commit()
    print("\nAll drops have been successfully added to the database.")
except Exception as e:
    session.rollback()
    print(f"\nAn error occurred: {e}")

session.close()
