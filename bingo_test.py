import time
from events.models.tasks import TaskType, BaseTask
from utils.format import format_number
from utils.redis import redis_client
from events.models import EventTask, BingoGameModel, EventTeamModel
from events.generators.BingoBoardGen import BingoBoard
import os
from db.models import ItemList, session, NpcList
        
def get_item_id(item_name: str) -> int:
    item = session.query(ItemList).filter(ItemList.item_name == item_name).filter(ItemList.noted == False).first()
    if not item:
        print(f"Item {item_name} not found")
        return None
    return item.item_id

def get_npc_id(npc_name: str) -> int:
    npc = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
    if not npc:
        print(f"NPC {npc_name} not found")
        return None
    return npc.npc_id

real = False

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
                ids = [[get_item_id(item) for item in task.task_config["items"]]]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "POINTS")
            elif task.task_config["requires"] == "all":
                ids = [get_item_id(item) for item in task.task_config["required_items"]]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ALL ITEMS")
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
            board.set_cell_npc_with_extras(i, j, task.task_config["source_npcs"], task.id, task.name, "KC TARGET")
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
                board.set_cell_npc_gp_target(i, j, [get_npc_id(npc) for npc in task.task_config["source_npcs"]], task.id, task.name, f"TOTAL LOOT")
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
    num = 45
    while os.path.exists("static/assets/img/bingo_board_{num}.png"):
        num += 1
    tasks = session.query(BaseTask).all()
    # Initialize grid position
    i, j = 0, 0
    # For a real event,
    if real:
        bingo_game = session.query(BingoGameModel).filter(BingoGameModel.event_id == 1).first()
        tasks = bingo_game.event.tasks
        # task_locations would be stored in a list of dicts, with the task_id as the key and the location as the value
        ## i,e [{"id": 1, "loc": (i, j)}]
        location_config = [config.config_value for config in bingo_game.event.configurations if config.config_key == "task_locations"]
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
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "FULL SET")
            elif task.task_config["requires"] == "points":
                ids = [[get_item_id(item) for item in task.task_config["items"]]]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "POINTS")
            elif task.task_config["requires"] == "all":
                ids = [get_item_id(item) for item in task.task_config["required_items"]]
                if real:
                    location_data = [loc for loc in location_config if loc["id"] == task.id]
                    i,j = location_data[0]["loc"]
                board.set_cell_items_with_extras(i, j, ids, task.id, task.name, "ALL ITEMS")
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
            board.set_cell_npc_with_extras(i, j, task.task_config["source_npcs"], task.id, task.name, "KC TARGET")
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
                board.set_cell_npc_gp_target(i, j, [get_npc_id(npc) for npc in task.task_config["source_npcs"]], task.id, task.name, f"TOTAL LOOT")
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

    board.draw_free_space_tile()
    board.save(f"static/assets/img/bingo_board_{num}.png")
    board.mark_cell_completed(0,0)
    board.mark_cell_completed(0,1)
    board.mark_cell_completed(0,3)
    board.mark_cell_completed(4,4)
    board.save(f"static/assets/img/bingo_board_completed_{num}.png")
    print(f"Saved number {num}")
