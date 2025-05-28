from sqlalchemy import event

from db.models import Session
from events.models import BaseTask, EventTask, AssignedTask, TaskType, TrackedTaskData
from db.models import NpcList
from utils import wiseoldman

@event.listens_for(Session, 'before_flush')
def handle_before_flush(session, flush_context, instances):
    for obj in session.new:
        if isinstance(obj, AssignedTask):
            target: AssignedTask = obj
            match target.task.task_type:
                case TaskType.ITEM_COLLECTION:
                    ## These are already stored properly
                    pass
                case TaskType.KC_TARGET:
                    target_npcs = target.task.task_config["source_npcs"]
                    if target_npcs[0] != "any":
                        for npc in target_npcs:
                            npc = session.query(NpcList).filter(NpcList.name == npc).first()
                            if npc:
                                target.task.task_config["npc_id"] = npc.id
                            else:
                                raise ValueError(f"NPC {npc} not found")
                        for player in target.team.members:
                            new_tracked_data = TrackedTaskData(
                                event_id=target.event_id,
                                team_id=target.team_id,
                                task_id=target.task_id,
                                player_id=player.player_id,
                                type=TaskType.KC_TARGET,
                                key="npc_id",
                                value=wiseoldman.get_player_metric_sync(player.player.player_name, target_npc) ## Use the string name of the npc
                            )
                            session.add(new_tracked_data)
                    else:
                        for player in target.team.members:
                            new_tracked_data = TrackedTaskData(
                                event_id=target.event_id,
                                team_id=target.team_id,
                                task_id=target.task_id,
                                player_id=player.player_id,
                                type=TaskType.KC_TARGET,
                                key="npc_id",
                                value=wiseoldman.get_player_total_kills(player.player.wom_id)
                            )
                            session.add(new_tracked_data)
                case TaskType.XP_TARGET:
                    target_skill = target.task.task_config["skill_name"]
                    for player in target.team.members:
                        new_tracked_data = TrackedTaskData(
                            event_id=target.event_id,
                            team_id=target.team_id,
                            task_id=target.task_id,
                            player_id=player.player_id,
                            type=TaskType.XP_TARGET,
                            key="skill_id", 
                            value=wiseoldman.get_player_metric_sync(player.player.player_name, target_skill)
                        )
                        session.add(new_tracked_data)
                case TaskType.EHP_TARGET:
                    for player in target.team.members:
                        new_tracked_data = TrackedTaskData(
                            event_id=target.event_id,
                            team_id=target.team_id,
                            task_id=target.task_id,
                            player_id=player.player_id,
                            type=TaskType.EHP_TARGET,
                            key="ehp",
                            value=wiseoldman.get_player_metric_sync(player.player.player_name, "ehp")
                        )
                        session.add(new_tracked_data)
                case TaskType.EHB_TARGET:
                    for player in target.team.members:
                        new_tracked_data = TrackedTaskData(
                            event_id=target.event_id,
                            team_id=target.team_id,
                            task_id=target.task_id,
                            player_id=player.player_id,
                            type=TaskType.EHB_TARGET,
                            key="ehb",
                            value=wiseoldman.get_player_metric_sync(player.player.player_name, "ehb")
                        )
                        session.add(new_tracked_data)
                case TaskType.LOOT_VALUE:
                    for player in target.team.members:
                        if target.task.task_config.get("source_npc", None) is not None:
                            target_npc = target.task.task_config["source_npc"]
                            npc = session.query(NpcList).filter(NpcList.name == target_npc).first()
                            if npc:
                                target.task.task_config["npc_id"] = npc.id
                            else:
                                raise ValueError(f"NPC {target_npc} not found")
                            player_starting_total = player.player.get_current_total(target.task.task_config["npc_id"], period="all_time")
                        else:
                            player_starting_total = player.player.get_current_total(period="all_time")
                        new_tracked_data = TrackedTaskData(
                            event_id=target.event_id,
                            team_id=target.team_id,
                            task_id=target.task_id,
                            player_id=player.player_id,
                            status="creation",
                            type=TaskType.LOOT_VALUE,
                            key="loot_value",
                            value=player_starting_total ## Use the all-time total for accuracy
                        )
                        session.add(new_tracked_data)

                case _:
                    raise ValueError(f"Task type {target.task.task_type} has no support for tracking-related data...?")
            session.commit()