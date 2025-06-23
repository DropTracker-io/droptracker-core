import time
from events.models.tasks import TaskType, BaseTask
from utils.format import convert_from_ms, format_number
from utils.redis import redis_client
from events.models import EventTask, BingoGameModel, EventTeamModel, BingoBoardTile, BingoBoardModel
from events.generators.BingoBoardGen import BingoBoard
import os
from db.models import ItemList, session, NpcList
from events.helpers.ids import get_item_id, get_npc_id


real = False

def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

async def regenerate_board(bingo_game: BingoGameModel, team_id: int = None):
    tasks = bingo_game.event.tasks
    # task_locations would be stored in a list of dicts, with the task_id as the key and the location as the value
    ## i,e [{"id": 1, "loc": (i, j)}]
    location_config = [config.config_value for config in bingo_game.event.configurations if config.config_key == "task_locations"]
    board = BingoBoard(size=5)
    for task in tasks:
        if task.task_type == TaskType.ITEM_COLLECTION:
            if task.task_config["requires"] == "set":
                sets: list[list[str]] = task.task_config["sets"]
                ids = [[get_item_id(item) for item in set] for set in sets]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "FULL SET")
            elif task.task_config["requires"] == "points":
                ids = [get_item_id(item) for item in task.task_config["items"]]
                # Chunk into rows of 5 for better display
                ids = list(chunk_list(ids, 5))
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "POINTS")
            elif task.task_config["requires"] == "all":
                items = {}
                for item, value in task.task_config["required_items"].items():
                    items[get_item_id(item)] = value
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, items, task.id, task.name, "ALL ITEMS")
            elif task.task_config["requires"] == "any":
                ids = [get_item_id(item) for item in task.task_config["required_items"]]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                    board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ANY ITEM")
                else:
                    print(f"Task {task.name} has no required items and is not a set-based task")
        elif task.task_type == TaskType.XP_TARGET:
            if real:
                location_data = [loc for loc in location_config if loc["id"] == task.id]
                i,j = location_data[0]["loc"]
            board.set_cell_skill_with_extras(i, j, [task.task_config["skill_name"]], task.id, task.name, "XP TARGET")
        elif task.task_type == TaskType.KC_TARGET:
            if real:
                location_data = [loc for loc in location_config if loc["id"] == task.id]
                i,j = location_data[0]["loc"]
            npcs = []
            for npc in task.task_config["source_npcs"]:
                if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                    print("Skipping npc", npc)
                    continue
                npcs.append(npc)
            kc_str = format_number(task.task_config["target_kc"]) + " "
            board.set_cell_npc_with_extras(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, "KC TARGET", kc_value=kc_str)
        elif task.task_type == TaskType.EHP_TARGET or task.task_type == TaskType.EHB_TARGET:
            if task.task_config.get("target_ehp", None) is not None:
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_skill_with_extras(i, j, ["ehp"], task.id, task.name, "EHP TARGET")
            elif task.task_config.get("target_ehb", None) is not None:
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_skill_with_extras(i, j, ["ehb"], task.id, task.name, "EHB TARGET")
        elif task.task_type == TaskType.LOOT_VALUE:
            gp_required = task.task_config.get("target_value", None)
            if gp_required is not None:
                gp_str = format_number(gp_required) + " "
            else:
                gp_str = ""
            if task.task_config.get("source_npcs", None) is not None:
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                npcs = []
                for npc in task.task_config["source_npcs"]:
                    if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                        print("Skipping npc", npc)
                        continue
                    npcs.append(npc)
                board.set_cell_npc_gp_target(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, f"TOTAL LOOT", gp_str)
            else:
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, [1004], task.id, task.name, f"TOTAL LOOT")
        elif task.task_type == TaskType.CUSTOM:
            pass
        i += 1
        if i >= 5:
            i = 0
            j += 1
        if j >= 5:
                    break
    if team_id is not None:
            ## Mark tiles completed for tasks this team has already finished
            team = session.query(EventTeamModel).filter(EventTeamModel.team_id == team_id).first()
            if team is not None:
                assigned_tasks = team.assigned_tasks
                for task in assigned_tasks:
                    if task.status == "completed":
                        if task.task_id in location_config:
                            i,j = location_config[task.task_id]["loc"]
                            board.mark_cell_completed(i, j)
    show_free_space = [config.config_value for config in bingo_game.event.configurations if config.config_key == "show_free_space"]
    if show_free_space:
        board.draw_free_space_tile()
    if team_id:
        board.save(f"static/assets/img/bingo_board_{team_id}.png")
    else:
        board.save(f"static/assets/img/bingo_board.png")


if __name__ == "__main__":  
    board = BingoBoard(size=5)
    num = 64
    while os.path.exists("static/assets/img/bingo_board_{num}.png"):
        num += 1
    tasks = session.query(BaseTask).all()
    # Initialize grid position
    i, j = 0, 0
    # For a real event, 
    real = True
    if real:
        print("Generating real board")
        bingo_game = session.query(BingoGameModel).filter(BingoGameModel.event_id == 3).first()
        board_model = session.query(BingoBoardModel).filter(BingoBoardModel.event_id == bingo_game.event_id).first()
        tiles = session.query(BingoBoardTile).filter(BingoBoardTile.board_id == board_model.board_id).all()
        for tile in tiles:
            print(f"Using tile {tile.task_id} for tile {tile.position_x}, {tile.position_y}")
            task = tile.task
            if task.task_type == TaskType.ITEM_COLLECTION:
                if task.task_config["requires"] == "set":
                    sets: list[list[str]] = task.task_config["sets"]
                    ids = [[get_item_id(item) for item in set] for set in sets]
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "FULL SET")
                elif task.task_config["requires"] == "points":
                    ids = [get_item_id(item) for item in task.task_config["items"]]
                    # Chunk into rows of 5 for better display
                    ids = list(chunk_list(ids, 5))
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "POINTS")
                elif task.task_config["requires"] == "all":
                    ids = [get_item_id(item) for item in task.task_config["required_items"]]
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ALL ITEMS")
                elif task.task_config["requires"] == "any":
                    ids = [get_item_id(item) for item in task.task_config["required_items"]]
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ANY ITEM")
                else:
                    print(f"Task {task.name} has no required items and is not a set-based task")
            elif task.task_type == TaskType.XP_TARGET:
                j, i = tile.position_x, tile.position_y
                board.set_cell_skill_with_extras(i, j, [task.task_config["skill_name"]], task.id, task.name, "XP TARGET")
            elif task.task_type == TaskType.KC_TARGET:
                j, i = tile.position_x, tile.position_y
                npcs = []
                for npc in task.task_config["source_npcs"]:
                    if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                        print("Skipping npc", npc)
                        continue
                    npcs.append(npc)
                kc_str = format_number(task.task_config["target_kc"]) + " "
                board.set_cell_npc_with_extras(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, "KC TARGET", kc_value=kc_str)
            elif task.task_type == TaskType.EHP_TARGET or task.task_type == TaskType.EHB_TARGET:
                if task.task_config.get("target_ehp", None) is not None:
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_skill_with_extras(i, j, ["ehp"], task.id, task.name, "EHP TARGET")
                elif task.task_config.get("target_ehb", None) is not None:
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_skill_with_extras(i, j, ["ehb"], task.id, task.name, "EHB TARGET")
            elif task.task_type == TaskType.LOOT_VALUE:
                gp_required = task.task_config.get("target_value", None)
                if gp_required is not None:
                    gp_str = format_number(gp_required) + " "
                else:
                    gp_str = ""
                if task.task_config.get("source_npcs", None) is not None:
                    j, i = tile.position_x, tile.position_y
                    npcs = []
                    for npc in task.task_config["source_npcs"]:
                        if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                            print("Skipping npc", npc)
                            continue
                        npcs.append(npc)
                    board.set_cell_npc_gp_target(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, f"TOTAL LOOT", gp_str)
                else:
                    j, i = tile.position_x, tile.position_y
                    board.set_cell_items_with_extras(i, j, [1004], task.id, task.name, f"TOTAL LOOT")
            elif task.task_type == TaskType.KILL_TIME:
                j, i = tile.position_x, tile.position_y
                board.set_cell_npc_time_target(i, j, [get_npc_id(task.task_config['target_npc'])], task.id, task.name, "KILL TIME", time_value=convert_from_ms(task.task_config["target_time"]))
            elif task.task_type == TaskType.CUSTOM:
                pass
            i += 1
            if i >= 5:
                i = 0
                j += 1
            if j >= 5:
                break

        board.draw_free_space_tile()
        board.save(f"static/assets/img/bingo_board_{num}.png")
        print(f"Generated real board from database with ID {num}")
        exit()
    for task in tasks:
        if i == 2 and j == 2:
            i += 1
            if i >= 5:
                i = 0
                j += 1
            if j >= 5:
                break
            continue
        if task.task_type == TaskType.ITEM_COLLECTION:
            if task.task_config["requires"] == "set":
                sets: list[list[str]] = task.task_config["sets"]
                ids = [[get_item_id(item) for item in set] for set in sets]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "FULL SET")
            elif task.task_config["requires"] == "points":
                ids = [get_item_id(item) for item in task.task_config["items"]]
                # Chunk into rows of 5 for better display
                ids = list(chunk_list(ids, 5))
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "POINTS")
            elif task.task_config["requires"] == "all":
                ids = [get_item_id(item) for item in task.task_config["required_items"]]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ALL ITEMS")
            elif task.task_config["requires"] == "any":
                ids = [get_item_id(item) for item in task.task_config["required_items"]]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ANY ITEM")
            else:
                print(f"Task {task.name} has no required items and is not a set-based task")
        elif task.task_type == TaskType.XP_TARGET:
            board.set_cell_skill_with_extras(i, j, [task.task_config["skill_name"]], task.id, task.name, "XP TARGET")
        elif task.task_type == TaskType.KC_TARGET:
            npcs = []
            for npc in task.task_config["source_npcs"]:
                if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                    print("Skipping npc", npc)
                    continue
                npcs.append(npc)
            kc_str = format_number(task.task_config["target_kc"]) + " "
            board.set_cell_npc_with_extras(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, "KC TARGET", kc_value=kc_str)
        elif task.task_type == TaskType.EHP_TARGET or task.task_type == TaskType.EHB_TARGET:
            if task.task_config.get("target_ehp", None) is not None:
                board.set_cell_skill_with_extras(i, j, ["ehp"], task.id, task.name, "EHP TARGET")
            elif task.task_config.get("target_ehb", None) is not None:
                board.set_cell_skill_with_extras(i, j, ["ehb"], task.id, task.name, "EHB TARGET")
        elif task.task_type == TaskType.LOOT_VALUE:
            gp_required = task.task_config.get("target_value", None)
            if gp_required is not None:
                gp_str = format_number(gp_required) + " "
            else:
                gp_str = ""
            if task.task_config.get("source_npcs", None) is not None:
                npcs = []
                for npc in task.task_config["source_npcs"]:
                    if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                        print("Skipping npc", npc)
                        continue
                    npcs.append(npc)
                board.set_cell_npc_gp_target(i, j, [get_npc_id(npc) for npc in npcs], task.id, task.name, f"TOTAL LOOT", gp_str)
            else:
                board.set_cell_items_with_extras(i, j, [1004], task.id, task.name, f"TOTAL LOOT")
        elif task.task_type == TaskType.KILL_TIME:
            board.set_cell_npc_time_target(i, j, [get_npc_id(task.task_config['target_npc'])], task.id, task.name, "KILL TIME", time_value=convert_from_ms(task.task_config["target_time"]))
        elif task.task_type == TaskType.CUSTOM:
            pass
        i += 1
        if i >= 5:
            i = 0
            j += 1
        if j >= 5:
            break

    board.draw_free_space_tile()
    board.save(f"static/assets/img/bingo_board_{num}.png")
    board.mark_cell_completed(0,0)
    board.mark_cell_completed(0,1)
    board.mark_cell_completed(0,3)
    board.mark_cell_completed(4,4)
    board.save(f"static/assets/img/bingo_board_completed_{num}.png")
    print(f"Saved number {num}")
