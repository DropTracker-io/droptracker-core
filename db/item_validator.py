## Helps to determine whether an item is able to be received from a specific loot source.

from osrsreboxed import items_api, monsters_api
from utils.redis import redis_client
from utils.logger import LoggerClient
import os

from dotenv import load_dotenv

load_dotenv()

monsters = monsters_api.load()
logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))



def check_item_against_monster(item_name, npc_name):

    if not npc_name:
        return False
    if npc_name == "Araxxor":
        return check_npc_with_no_table(npc_name, item_name)
    target_npcs = []
    for monster in monsters:
        if monster.name.lower() == npc_name.lower():
            target_npcs.append(monster)
    if not target_npcs:
        #logger.log("warning", "NPC not found in OSRS API, drop validated regardless..", {"npc_name": npc_name})
        return True
    total_targets = len(target_npcs)
    for npc in target_npcs:
        if npc.drops == []:
            print("NPC", npc.name, "has no drops")
            total_targets -= 1
            continue
        for drop in npc.drops:
            if drop.name.lower() == item_name.lower():
                return True
    if total_targets == 0:
        print("This NPC has no drops stored in osrsreboxed, validating regardles...")
        return True
    print("This drop is not expected from this npc:", item_name, "from", npc_name + "... we checked against", len(target_npcs), "osrsreboxed npcs")
    return False


def check_npc_with_no_table(npc_name, item_name):
    araxxor_table = [
    "Noxious pommel",
    "Noxious point",
    "Noxious blade",
    "Araxyte fang",
    "Araxyte venom sack",
    "Super combat potion (1)",
    "Prayer potion (4)",
    "Shark",
    "Wild pie",
    "Dragon platelegs",
    "Dragon mace",
    "Rune kiteshield",
    "Rune platelegs",
    "Rune 2h sword",
    "Death rune",
    "Nature rune",
    "Mud rune",
    "Blood rune",
    "Magic seed",
    "Yew seed",
    "Toadflax seed",
    "Ranarr seed",
    "Snapdragon seed",
    "Spirit seed",
    "Coal",
    "Adamantite ore",
    "Raw shark",
    "Yew logs",
    "Runite ore",
    "Raw monkfish",
    "Pure essence",
    "Spider cave teleport",
    "Earth orb",
    "Mort myre fungus",
    "Antidote++(3)",
    "Wine of zamorak",
    "Red spiders' eggs",
    "Bark",
    "Araxyte head",
    "Jar of venom",
    "Nid",
    "Brimstone key",
    "Clue scroll (elite)",
    "Coagulated venom",
    ]
    if npc_name == "Araxxor":
        if item_name in araxxor_table:
            return True
    return False
