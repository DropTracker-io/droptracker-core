from events.models import BaseTask
from events.generators import BingoBoardGen
from events.models.tasks import TaskType
from events.helpers.ids import get_item_id, get_npc_id
from utils.format import convert_from_ms, format_number

async def get_bingo_task_tile_image(task_obj: BaseTask) -> str:
    print(f"\nStarting get_bingo_task_tile_image")
    print(f"Task object: id={task_obj.id}, type={task_obj.task_type}, name={task_obj.name}")
    print(f"Task config: {task_obj.task_config}")
    
    board = BingoBoardGen.BingoBoard(size=1)
    success = False
    tile_url = None
    
    print(f"Task type enum value: {task_obj.task_type.value}")
    print(f"Task type enum name: {task_obj.task_type.name}")
    
    match task_obj.task_type:
        case TaskType.ITEM_COLLECTION:
            print("Processing ITEM_COLLECTION task")
            if task_obj.task_config["requires"] == "set":
                print("Processing set-based task")
                sets = [[get_item_id(item) for item in set] for set in task_obj.task_config["sets"]]
                print(f"Tile item IDs (set): {sets}")
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="FULL SET",
                    item_ids=sets
                )
            elif task_obj.task_config["requires"] == "points":
                print("Processing points-based task")
                items = task_obj.task_config["items"]
                if isinstance(items, dict):
                    item_ids = [get_item_id(item) for item in items.keys()]
                else:
                    item_ids = [get_item_id(item) for item, _ in items]
                print(f"Tile item IDs (points): {item_ids}")
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="POINTS",
                    item_ids=item_ids
                )
            elif task_obj.task_config["requires"] == "all":
                print("Processing all-items task")
                items = task_obj.task_config["required_items"]
                if isinstance(items, dict):
                    item_ids = [get_item_id(item) for item in items.keys()]
                else:
                    item_ids = [get_item_id(item) for item in items]
                print(f"Tile item IDs (all): {item_ids}")
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="ALL ITEMS",
                    item_ids=item_ids
                )
            elif task_obj.task_config["requires"] == "any":
                print("Processing any-item task")
                items = task_obj.task_config["required_items"]
                if isinstance(items, dict):
                    item_ids = [get_item_id(item) for item in items.keys()]
                else:
                    item_ids = [get_item_id(item) for item in items]
                print(f"Tile item IDs (any): {item_ids}")
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="ANY ITEM",
                    item_ids=item_ids
                )
        case TaskType.XP_TARGET:
            print("Processing XP_TARGET task")
            success = board.create_single_tile(
                task_type=task_obj.task_type.value,
                task_id=task_obj.id,
                task_name=task_obj.name,
                badge="XP TARGET",
                skill_names=[task_obj.task_config["skill_name"]]
            )
        case TaskType.KC_TARGET:
            print("Processing KC_TARGET task")
            npcs = []
            for npc in task_obj.task_config["source_npcs"]:
                if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                    continue
                npcs.append(npc)
            kc_str = format_number(task_obj.task_config["target_kc"]) + " "
            success = board.create_single_tile(
                task_type=task_obj.task_type.value,
                task_id=task_obj.id,
                task_name=task_obj.name,
                badge="KC TARGET",
                npc_ids=[get_npc_id(npc) for npc in npcs],
                kc_value=kc_str
            )
        case TaskType.LOOT_VALUE:
            print("Processing LOOT_VALUE task")
            gp_required = task_obj.task_config.get("target_value", None)
            gp_str = format_number(gp_required) + " " if gp_required is not None else ""
            if task_obj.task_config.get("source_npcs", None) is not None:
                npcs = []
                for npc in task_obj.task_config["source_npcs"]:
                    if any(existing_npc[:7] == npc[:7] for existing_npc in npcs):
                        continue
                    npcs.append(npc)
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="TOTAL LOOT",
                    npc_ids=[get_npc_id(npc) for npc in npcs],
                    gp_value=gp_str
                )
            else:
                success = board.create_single_tile(
                    task_type=task_obj.task_type.value,
                    task_id=task_obj.id,
                    task_name=task_obj.name,
                    badge="TOTAL LOOT",
                    item_ids=[1004],
                    gp_value=gp_str
                )
        case TaskType.EHP_TARGET:
            print("Processing EHP_TARGET task")
            success = board.create_single_tile(
                task_type=task_obj.task_type.value,
                task_id=task_obj.id,
                task_name=task_obj.name,
                badge="EHP TARGET",
                skill_names=["ehp"]
            )
        case TaskType.EHB_TARGET:
            print("Processing EHB_TARGET task")
            success = board.create_single_tile(
                task_type=task_obj.task_type.value,
                task_id=task_obj.id,
                task_name=task_obj.name,
                badge="EHB TARGET",
                skill_names=["ehb"]
            )
        case TaskType.KILL_TIME:
            print("Processing KILL_TIME task")
            success = board.create_single_tile(
                task_type=task_obj.task_type.value,
                task_id=task_obj.id,
                task_name=task_obj.name,
                badge="KILL TIME",
                npc_ids=[get_npc_id(task_obj.task_config["target_npc"])],
                time_value=convert_from_ms(task_obj.task_config["target_time"])
            )
        case _:
            print(f"Unhandled task type: {task_obj.task_type}")
            raise ValueError(f"Unhandled task type: {task_obj.task_type}")

    # Get the tile image URL
    if success:
        tile_url = success
        print(f"Successfully generated tile URL: {tile_url}")
    else:
        tile_url = None
        print(f"Failed to generate tile URL")
    return tile_url