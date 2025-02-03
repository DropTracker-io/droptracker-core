import os
from db.models import session, Group, NpcList, User, Player, user_group_association, PersonalBestEntry
from sqlalchemy import select, func
from sqlalchemy.orm import aliased
from interactions import Embed

from utils.format import convert_from_ms, get_npc_image_url

async def get_group_pbs(boss_name, group_id):
    # Validate the inputs
    if not group_id or not boss_name:
        return {"error": "A group ID and boss name must be provided."}

    # Step 1: Get npc_ids for the given boss name
    npc_ids = session.query(NpcList.npc_id).filter(NpcList.npc_name == boss_name).all()
    npc_id_list = [npc_id[0] for npc_id in npc_ids]
    thumb_url = None
    for npcid in npc_id_list:
        if not thumb_url:
            thumb_url = await get_npc_image_url(boss_name, npcid)
            if os.path.exists(f'/store/droptracker/disc/static/assets/img/npcdb/{npcid}.png'):
                thumb_url = f"https://www.droptracker.io/img/npcdb/{npcid}.png"
    if not thumb_url:
        thumb_url = "https://www.droptracker.io/img/droptracker-small.gif"
    # Step 2: Conditional query logic based on group_id
    if group_id == 2:
        # Global ranking: query PersonalBestEntry objects for the specified npc_id list, ignoring group filtering
        pbs_query = (
            session.query(
                PersonalBestEntry.player_id,
                PersonalBestEntry.npc_id,
                PersonalBestEntry.personal_best.label('best_time_seconds'),  # Use `personal_best` for ranking
                Player.player_name,
                User.username
            )
            .join(Player, PersonalBestEntry.player_id == Player.player_id)
            .outerjoin(User, User.user_id == Player.user_id)
            .filter(PersonalBestEntry.npc_id.in_(npc_id_list))
            .order_by(PersonalBestEntry.personal_best.asc())
        )
    else:
        # Group-specific ranking: query only PersonalBestEntry objects for players in the specified group
        # Step 2a: Get player IDs in the specified group
        query = select(Player.player_id, Player.player_name, User.username) \
            .select_from(user_group_association) \
            .join(Player, Player.player_id == user_group_association.c.player_id) \
            .outerjoin(User, User.user_id == Player.user_id) \
            .where(user_group_association.c.group_id == group_id)

        players_result = session.execute(query)
        players = players_result.fetchall()
        player_ids = [player.player_id for player in players]

        # Step 2b: Query PersonalBestEntry objects for the specified npc_id list and group player IDs
        pbs_query = (
            session.query(
                PersonalBestEntry.player_id,
                PersonalBestEntry.npc_id,
                PersonalBestEntry.personal_best.label('best_time_seconds'),  # Use `personal_best` for ranking
                Player.player_name,
                User.username
            )
            .join(Player, PersonalBestEntry.player_id == Player.player_id)
            .outerjoin(User, User.user_id == Player.user_id)
            .filter(
                PersonalBestEntry.player_id.in_(player_ids),
                PersonalBestEntry.npc_id.in_(npc_id_list)
            )
            .order_by(PersonalBestEntry.personal_best.asc())
        )

    # Step 3: Execute the query and build structured output with rankings
    pbs_results = pbs_query.all()
    ranked_pbs = []
    for rank, pb in enumerate(pbs_results, start=1):
        ranked_pbs.append({
            "rank": rank,
            "player_id": pb.player_id,
            "player_name": pb.player_name,
            "personal_best_seconds": convert_from_ms(pb.best_time_seconds)
        })

    # Step 4: Return the structured result
    print("There are", len(ranked_pbs), "pbs for", boss_name)
    return {"group_pbs": ranked_pbs,
            "thumb_url": thumb_url}


pb_npc_list = [
    "Alchemical Hydra",
    "Araxxor",
    "Chambers of Xeric",
    "Duke Sucellus",
    "Grotesque Guardians",
    "Nightmare",
    "Phantom Muspah",
    "Sol Heredit",
    "The Gauntlet",
    "The Corrupted Gauntlet",
    "The Leviathan",
    "The Whisperer",
    "Theatre of Blood",
    "Tombs of Amascut",
    "Vardorvis",
    "Vorkath",
    "Zulrah"
]

async def create_pb_embeds(group_id, included_list, max_entries=5):
    embeds = []
    for npc in included_list:
        if npc not in pb_npc_list:
            ## Entry validation logic
            continue
        # Get the group's personal bests for the current NPC
        group_pbs_response = await get_group_pbs(npc, group_id)
        
        if "error" in group_pbs_response:
            continue  # Skip if there's an error in the response
        
        group_pbs = group_pbs_response.get("group_pbs", [])
        thumbnail = group_pbs_response.get("thumb_url", "https://www.droptracker.io/img/droptracker-small.gif")
        
        if not group_pbs:
            continue  # Skip if there are no personal bests for this NPC

        # Limit the number of PB entries to max_entries
        limited_pbs = group_pbs[:max_entries]
        
        # Create an embed for this NPC
        embed = Embed(
            title=f"{npc}",
            description=f"Top {max_entries} Personal Bests" if max_entries > 1 else "Top Personal Best"
        )
        embed.set_thumbnail(url=thumbnail)
        
        # Add each PB as a field in the embed
        for pb in limited_pbs:
            embed.add_field(
                name=f"#{pb['rank']} - {pb['player_name']}",
                value=f"Time: {pb['personal_best_seconds']} seconds",
                inline=False
            )
        
        embeds.append(embed)
    
    return embeds
