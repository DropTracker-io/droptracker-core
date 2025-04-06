import calendar
import interactions
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session
from db.models import Group, Player, GroupConfiguration, LootboardStyle, NpcList, session
from utils.redis import redis_client
from PIL import ImageFont
import json
from utils.dynamic_handling import get_dynamic_color, get_value_color
from lootboard.generator import load_background_image, draw_headers, draw_recent_drops, draw_drops_on_image, save_image, generate_time_partitions
from utils.format import format_number

yellow = (255, 255, 0)
black = (0, 0, 0)
font_size = 26

rs_font_path = "static/assets/fonts/runescape_uf.ttf"
tracker_fontpath = 'static/assets/fonts/droptrackerfont.ttf'
main_font = ImageFont.truetype(rs_font_path, font_size)

async def generate_player_board(bot: interactions.Client, player_id: int, start_time: datetime = None, end_time: datetime = None, npc_limit: int = 10):
    """
    Generate a loot board for a specific player, ranking the NPCs they have loot from instead of players.
    
    :param bot: Instance of the interactions.Client bot object
    :param player_id: The player ID to generate the board for
    :param start_time: Start datetime for the timeframe (inclusive)
    :param end_time: End datetime for the timeframe (inclusive)
    :param npc_limit: Maximum number of NPCs to display in the ranking
    :return: Path to the generated image
    """
    # Set default times if not provided
    if start_time is None:
        # Default to start of current month
        start_time = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    if end_time is None:
        # Default to current time
        end_time = datetime.now()
    
    # Determine the appropriate time granularity based on the timeframe
    time_diff = end_time - start_time
    if time_diff.days > 30:
        # For timeframes longer than a month, use monthly partitions
        granularity = "monthly"
    elif time_diff.days > 1:
        # For timeframes longer than a day, use daily partitions
        granularity = "daily"
    elif time_diff.seconds > 3600:
        # For timeframes longer than an hour, use hourly partitions
        granularity = "hourly"
    else:
        # For shorter timeframes, use minute partitions
        granularity = "minute"
    
    # Get player information
    player = session.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        print(f"Cannot generate a lootboard, player ID {player_id} not found")
        return None
    
    # Get player's group for configuration
    player_group_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id AND group_id != 2 LIMIT 1"""
    player_group = session.execute(text(player_group_query), {"player_id": player_id}).first()
    group_id = player_group[0] if player_group else 2  # Default to global group if no specific group
    
    # Get group configuration
    group_config = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).all()
    config = {conf.config_key: conf.config_value for conf in group_config}
    
    # Get lootboard style and minimum value
    loot_board_style = int(config.get('loot_board_type', 1))
    minimum_value = int(config.get('minimum_value_to_notify', 2500000))
    
    # Load background image
    target_board = session.query(LootboardStyle).filter(LootboardStyle.id == loot_board_style).first()
    local_url = target_board.local_url if target_board else "/store/droptracker/disc/lootboard/bank-new-clean-dark.png"
    bg_img, draw = load_background_image(local_url)
    
    # Get dynamic color settings
    use_dynamic_colors = config.get('use_dynamic_lootboard_colors', True)
    if use_dynamic_colors and use_dynamic_colors == "1":
        use_dynamic_colors = True
    else:
        use_dynamic_colors = False
    use_gp_colors = config.get('use_gp_colors', True)
    
    # Generate time partitions to query
    time_partitions = generate_time_partitions(start_time, end_time, granularity)
    print(f"Got {len(time_partitions)} time partitions for player {player.player_name}")
    
    # Get the NPC totals, items, and recent drops for the player
    npc_totals, group_items, recent_drops, total_loot = await get_player_npc_drops(
        player_id, time_partitions, granularity
    )
    
    # Draw elements on the background image
    bg_img = await draw_drops_on_image(bg_img, draw, group_items, group_id, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Create a timeframe string for the header
    timeframe_str = f"{player.player_name} - {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}"
    
    # Draw headers with custom timeframe string
    bg_img = await draw_player_headers(bot, player.player_name, total_loot, bg_img, draw, timeframe_str, 
                               dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Draw recent drops
    bg_img = await draw_recent_drops(bg_img, draw, recent_drops, min_value=minimum_value, 
                                    dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Draw NPC leaderboard instead of player leaderboard
    bg_img = await draw_npc_leaderboard(bg_img, draw, npc_totals, npc_limit, 
                                       dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Save the image with a custom filename
    timeframe_id = f"player{player_id}-{start_time.strftime('%Y%m%d')}-{end_time.strftime('%Y%m%d')}"
    image_path = save_image(bg_img, group_id, timeframe_id)
    return image_path


async def draw_player_headers(bot: interactions.Client, player_name, total_loot, bg_img, draw, partition=None, *, dynamic_colors, use_gp):
    """
    Draw headers on the image, including the title and total loot value.
    The total loot value is displayed using a dynamic color based on its numeric value.
    """
    # Determine if we're using a daily partition.
    is_daily = partition and '-' in str(partition)
    if is_daily:
        try:
            date_obj = datetime.strptime(partition, '%Y-%m-%d')
            date_display = date_obj.strftime('%B %d, %Y')
        except Exception:
            date_display = partition
    else:
        current_month = datetime.now().month
        date_display = calendar.month_name[current_month].capitalize()
    
    # Format total loot for display and compute dynamic color using the numeric value.
    this_month_str = format_number(total_loot)
    value_text_color = get_value_color(total_loot) # (added BY Smoke)
    
    prefix = f"{player_name}'s Tracked Drops - ({date_display}) - "
    
    # Calculate widths for centering the entire header.
    prefix_bbox = draw.textbbox((0, 0), prefix, font=main_font)
    prefix_width = prefix_bbox[2] - prefix_bbox[0]
    value_bbox = draw.textbbox((0, 0), this_month_str, font=main_font)
    value_width = value_bbox[2] - value_bbox[0]
    total_width = prefix_width + value_width

    bg_img_w, _ = bg_img.size
    head_loc_x = int((bg_img_w - total_width) / 2)
    head_loc_y = 20  # Adjust this if needed.
    if dynamic_colors:
        text_color = get_dynamic_color(bg_img)
    else:
        text_color = yellow
    # Draw the prefix with a fixed text color (e.g. yellow) and a thicker stroke. (color adjustments added BY Smoke)
    draw.text((head_loc_x, head_loc_y), prefix, font=main_font,
              fill=text_color, stroke_width=2, stroke_fill=black)
    # Draw the total loot value using the dynamic value_text_color.
    draw.text((head_loc_x + prefix_width, head_loc_y), this_month_str, font=main_font,
              fill=value_text_color, stroke_width=1, stroke_fill=black)
    return bg_img

async def get_player_npc_drops(player_id, time_partitions, granularity):
    """
    Returns the NPC totals, items, and recent drops for a specific player
    across multiple time partitions.
    """
    npc_totals = {}
    group_items = {}
    recent_drops = []
    total_loot = 0
    
    # Determine Redis key prefix based on granularity
    if granularity == "monthly":
        prefix = ""  # Monthly has no prefix
    elif granularity == "daily":
        prefix = "daily"
    elif granularity == "hourly":
        prefix = "hourly"
    else:
        prefix = "minute"
    
    for partition in time_partitions:
        # Process NPC totals
        if prefix:
            # For non-monthly granularity, we need to check each NPC individually
            npc_key = f"player:{player_id}:{prefix}:{partition}:npcs"
            npc_data = redis_client.client.hgetall(npc_key)
            for npc_id_bytes, value_bytes in npc_data.items():
                npc_id = npc_id_bytes.decode('utf-8')
                value = int(value_bytes.decode('utf-8'))
                
                if npc_id in npc_totals:
                    npc_totals[npc_id] += value
                else:
                    npc_totals[npc_id] = value
                
                total_loot += value
        else:
            # For monthly granularity, we can use the npc_totals hash
            npc_key = f"player:{player_id}:{partition}:npc_totals"
            npc_data = redis_client.client.hgetall(npc_key)
            for npc_id_bytes, value_bytes in npc_data.items():
                npc_id = npc_id_bytes.decode('utf-8')
                value = int(value_bytes.decode('utf-8'))
                
                if npc_id in npc_totals:
                    npc_totals[npc_id] += value
                else:
                    npc_totals[npc_id] = value
                
                total_loot += value
        
        # Process items
        if prefix:
            total_items_key = f"player:{player_id}:{prefix}:{partition}:items"
        else:
            total_items_key = f"player:{player_id}:{partition}:total_items"
        
        total_items = redis_client.client.hgetall(total_items_key)
        for key_bytes, value_bytes in total_items.items():
            key = key_bytes.decode('utf-8')
            value = value_bytes.decode('utf-8')
            try:
                quantity, total_value = map(int, value.split(','))
            except ValueError:
                continue
            
            if key in group_items:
                existing_quantity, existing_value = map(int, group_items[key].split(','))
                new_quantity = existing_quantity + quantity
                new_total_value = existing_value + total_value
                group_items[key] = f"{new_quantity},{new_total_value}"
            else:
                group_items[key] = f"{quantity},{total_value}"
        
        # Process recent drops
        if prefix:
            recent_items_key = f"player:{player_id}:{prefix}:{partition}:recent_items"
        else:
            recent_items_key = f"player:{player_id}:{partition}:recent_items"
        
        recent_items_raw = redis_client.client.lrange(recent_items_key, 0, -1)
        for item_bytes in recent_items_raw:
            item_data = json.loads(item_bytes.decode('utf-8'))
            # Add to recent drops if not already present
            if not any(drop['drop_id'] == item_data['drop_id'] for drop in recent_drops):
                recent_drops.append(item_data)
    
    # Sort recent drops by date (newest first)
    recent_drops.sort(key=lambda x: x['date_added'], reverse=True)
    
    # Limit to most recent 10 drops
    recent_drops = recent_drops[:10]
    
    return npc_totals, group_items, recent_drops, total_loot


async def draw_npc_leaderboard(bg_img, draw, npc_totals, npc_limit=10, dynamic_colors=False, use_gp=False):
    """
    Draw the NPC leaderboard on the image in the same position as the player leaderboard.
    """
    # Sort NPCs by total loot value
    sorted_npcs = sorted(npc_totals.items(), key=lambda x: x[1], reverse=True)[:npc_limit]
    
    # Get NPC names from database
    npc_ids = [int(npc_id) for npc_id, _ in sorted_npcs]
    npcs = session.query(NpcList).filter(NpcList.npc_id.in_(npc_ids)).all()
    npc_name_map = {str(npc.npc_id): npc.npc_name for npc in npcs}
    
    # Define text positioning (same as draw_leaderboard)
    name_x = 141
    name_y = 228
    pet_font = ImageFont.truetype(rs_font_path, 15)
    first_name = True
    
    # Define colors
    if dynamic_colors:
        text_color = get_dynamic_color(bg_img)
    else:
        text_color = yellow
    
    # Draw each NPC in the ranking
    for i, (npc_id, total) in enumerate(sorted_npcs):
        # Get NPC name
        npc_name = npc_name_map.get(npc_id, f"Unknown NPC ({npc_id})")
        
        # Format rank and total
        rank_num_text = f'{i + 1}'
        if len(npc_name) > 15:  # Truncate long NPC names
            npc_name = npc_name[:12] + "..."
        rsn_text = npc_name
        
        # Format total value
        total_loot_display = format_number(total)
        gp_text = f'{total_loot_display}'
        
        # Determine positions for rank, name, and total loot text
        rank_x, rank_y = (name_x - 104), name_y
        quant_x, quant_y = (name_x + 106), name_y
        
        # Calculate center for loot (gp_text) and rank_num_text
        quant_bbox = draw.textbbox((0, 0), gp_text, font=pet_font)
        center_q_x = quant_x - (quant_bbox[2] - quant_bbox[0]) / 2
        
        rsn_bbox = draw.textbbox((0, 0), rsn_text, font=pet_font)
        center_x = name_x - (rsn_bbox[2] - rsn_bbox[0]) / 2
        
        rank_bbox = draw.textbbox((0, 0), rank_num_text, font=pet_font)
        rank_mid_x = rank_x - (rank_bbox[2] - rank_bbox[0]) / 2
        
        # Draw text for rank, name, and total loot
        draw.text((center_x, name_y), rsn_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)
        draw.text((rank_mid_x, rank_y), rank_num_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)
        draw.text((center_q_x, quant_y), gp_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)
        
        # Update Y position for the next NPC
        if not first_name:
            name_y += 22
        else:
            name_y += 22
            first_name = False
    
    return bg_img
