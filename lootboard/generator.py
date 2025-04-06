import asyncio
import csv
import calendar
import json
import os
import traceback
from datetime import datetime, timedelta
from db.models import Drop, Guild, LootboardStyle, Player, ItemList, session, Group, GroupConfiguration, NpcList
from io import BytesIO

import aiohttp
import interactions
from PIL import Image, ImageFont, ImageDraw

from utils.redis import RedisClient
from utils.wiseoldman import fetch_group_members
from db.ops import DatabaseOperations, associate_player_ids

from utils.format import format_number
from utils.dynamic_handling import get_value_color, get_dynamic_color, get_coin_image_id

redis_client = RedisClient()
db = DatabaseOperations()
yellow = (255, 255, 0)
black = (0, 0, 0)
font_size = 26

rs_font_path = "static/assets/fonts/runescape_uf.ttf"
tracker_fontpath = 'static/assets/fonts/droptrackerfont.ttf'
main_font = ImageFont.truetype(rs_font_path, font_size)

async def get_drops_for_group(player_ids, partition: str):
    """ Returns the drops stored in redis cache 
        for the specific list of player_ids
    """
    group_items = {}
    recent_drops = []
    total_loot = 0
    player_totals = {}
    
    # Determine if we're using a daily partition (contains a dash) or monthly partition
    
    async def process_player(player_id):
        nonlocal total_loot
        player_total = 0
        
        # Use different key format based on partition type
        if len(str(partition)) > 6:
            ## Anything with a partition greater than 6 digits is a special type...
            ## A "Daily" partition would come in as 20250327
            ## An "hourly" partition would come in as 2025032712 (12th hour of the 27th of March 2025)
            ## A "minute" partition would come in as 202503271234 (12:34th minute of the 27th of March 2025)
            ## Here, we split them into year, month, day
            ## We are using something more specific than the daily partitions...
            pass
        else:
            # Monthly format: player:123:202503:total_items
            total_items_key = f"player:{player_id}:{partition}:total_items"
            recent_items_key = f"player:{player_id}:{partition}:recent_items"
            loot_key = f"player:{player_id}:{partition}:total_loot"

        # Get total items
        total_items = redis_client.client.hgetall(total_items_key)
        
        for key, value in total_items.items():
            key = key.decode('utf-8')
            value = value.decode('utf-8')
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
                
        player_total = redis_client.client.get(loot_key)
        if player_total:
            player_total = int(player_total.decode('utf-8'))
        else:
            player_total = 0
            
        # Get recent items and ensure uniqueness
        if len(str(partition)) > 6:
            # Get all recent items first
            all_recent_items = [json.loads(item.decode('utf-8')) for item in redis_client.client.lrange(recent_items_key, 0, -1)]
            # Filter to only include items from the specified day
            target_date = partition  # Format: 2025-03-04
            recent_items = list({item['date_added']: item for item in all_recent_items 
                               if item['date_added'].startswith(target_date)}.values())
        else:
            recent_items = list({json.loads(item.decode('utf-8'))['date_added']: json.loads(item.decode('utf-8')) 
                            for item in redis_client.client.lrange(recent_items_key, 0, -1)}.values())
        
        return player_id, player_total, recent_items

    # Use asyncio.gather to process all players concurrently
    results = await asyncio.gather(*[process_player(player_id) for player_id in player_ids])

    for player_id, player_total, player_recent_items in results:
        player_totals[player_id] = player_total
        total_loot += player_total
        recent_drops.extend(player_recent_items)

    return group_items, player_totals, recent_drops, total_loot


async def generate_server_board(bot: interactions.Client, group_id: int = 0, wom_group_id: int = 0, partition: str = None):
    """
        :param: bot: Instance of the interactions.Client bot object
        :param: group_id: DropTracker GroupID. 0 expects a wom_group_id
        :param: wom_group_id: WiseOldMan groupID. 0 expects a group_id
        :param: partition: The partition to search drops for (202408 for August 2024 or 2025-03-04 for daily)
        Providing neither option (group_id, wom_group_id) uses global drops.
    """
    # Set default partition if none provided
    if partition is None:
        partition = datetime.now().year * 100 + datetime.now().month
    
    # Determine if we're using a daily partition
    if len(str(partition)) > 6:
        ## Our normal partitioning always uses 6 digits -- YYYYMM
        is_daily = True
    else:
        is_daily = False
    
    group = None
    if group_id != 0: ## we prioritize group_id here
        group = session.query(Group).filter(Group.group_id == group_id).first()
    elif wom_group_id != 0:
        group = session.query(Group).filter(Group.wom_id == wom_group_id).first()
    
    if (group_id != 0 or wom_group_id != 0) and not group:
        print("Cannot generate a lootboard, no group data was properly parsed..")
    elif (group_id == 0 and wom_group_id == 0):
        group_id = 1
    else:
        group_id = group.group_id
    
    group_config = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).all()
    # Transform group_config into a dictionary for easy access
    config = {conf.config_key: conf.config_value for conf in group_config}

    #loot_board_style = 1  # TODO: Implement other boards eventually
    loot_board_style = config.get('loot_board_type', 1)
    #print(f"Lootboard style: {loot_board_style}")
    minimum_value = config.get('minimum_value_to_notify', 2500000)
    player_wom_ids = []
    # Load background image based on the board style
    loot_board_style = int(loot_board_style)
    target_board = session.query(LootboardStyle).filter(LootboardStyle.id == loot_board_style).first()
    local_url = target_board.local_url
    if not target_board:
        local_url = "/store/droptracker/disc/lootboard/bank-new-clean-dark.png"

    bg_img, draw = load_background_image(local_url)

    # Compute the dynamic text color based on the background image. (added BY Smoke)
    use_dynamic_colors = config.get('use_dynamic_lootboard_colors', True)
    if use_dynamic_colors and use_dynamic_colors == "1":
        use_dynamic_colors = True
    else:
        use_dynamic_colors = False
    use_gp_colors = config.get('use_gp_colors', True)
    #print(f"Dynamic color selected: {dynamic_color}")
    
    #f"Group ID: {group_id}")
    if group_id != 2:
        if wom_group_id == 0:
            wom_group_id = group.wom_id
        elif wom_group_id != 0:
    # Fetch player WOM IDs and associated Player IDs
            player_wom_ids = await fetch_group_members(wom_group_id)
        else:
            player_wom_ids = []
            raw_wom_ids = session.query(Player.wom_id).all() ## get all users if no wom_group_id is found
            for player in raw_wom_ids:
                player_wom_ids.append(player[0])
    else:
        #print("Group ID is 2...")
        player_wom_ids = []
        all_players = session.query(Player.wom_id).all()
        #print(f"Got all players: {len(all_players)}")
        for p in all_players:
            player_wom_ids.append(p.wom_id)
    player_ids = await associate_player_ids(player_wom_ids)
    # Get the drops, recent drops, and total loot for the group
    #print("Processed player_ids")
    group_items, player_totals, recent_drops, total_loot = await get_drops_for_group(player_ids, partition)

    # Draw elements on the background image (added dynamic_coloring added BY Smoke)
    bg_img = await draw_drops_on_image(bg_img, draw, group_items, group_id, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)  # Pass `group_items` here
    bg_img = await draw_headers(bot, group_id, total_loot, bg_img, draw, partition, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)  # Draw headers
    bg_img = await draw_recent_drops(bg_img, draw, recent_drops, min_value=minimum_value, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)  # Draw recent drops, with a minimum value
    bg_img = await draw_leaderboard(bg_img, draw, player_totals, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)  # Draw leaderboard
    save_image(bg_img, group_id, partition)  # Save the generated image
    #print("Saved the new image.")
    
    # When saving the image, use a different naming convention for daily partitions
    if is_daily:
        # For daily partitions, use the date directly
        file_path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/daily_{partition.replace('-', '')}.png"
    else:
        # For monthly partitions, use the existing format
        current_date = datetime.now()
        ydmpart = int(current_date.strftime('%d%m%Y'))
        file_path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/{ydmpart}.png"
    
    return file_path


def get_year_month_string():
    return datetime.now().strftime('%Y-%m')


async def draw_headers(bot: interactions.Client, group_id, total_loot, bg_img, draw, partition=None, *, dynamic_colors, use_gp):
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
    
    # Build the header prefix.
    if int(group_id) == 2:
        prefix = f"Tracked Drops - All Players ({date_display}) - "
    else:
        group = session.query(Group).filter(Group.group_id == group_id).first()
        server_name = group.group_name
        prefix = f"{server_name}'s Tracked Drops for {date_display} - "
    
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



def load_background_image(filepath):
    bg_img = Image.open(filepath)
    draw = ImageDraw.Draw(bg_img)
    return bg_img, draw


async def draw_leaderboard(bg_img, draw, player_totals, *, dynamic_colors, use_gp):
    """
    Draws the leaderboard for players with their total loot values.
    
    :param bg_img: The background image to draw the leaderboard on.
    :param draw: The ImageDraw object used to draw the text.
    :param player_totals: Dictionary of player names and their total loot value.
    :return: Updated background image with the leaderboard drawn on it.
    """
    # Sort players by total loot value in descending order, taking the top 12
    top_players = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)[:12]
    
    # Define text positioning
    name_x = 141
    name_y = 228
    pet_font = ImageFont.truetype(rs_font_path, 15)
    first_name = True

    for i, (player, total) in enumerate(top_players):
        # Format player loot totals
        # total_value = int(total)
        total_loot_display = format_number(total)

        # Create rank, name, and loot text
        rank_num_text = f'{i + 1}'
        player_obj = session.query(Player.player_name).filter(Player.player_id == player).first()
        if not player_obj:
            #print("Player with ID", player, " not found.")
            player_rsn = f"Name not found...."
        else:
            player_rsn = player_obj.player_name
        rsn_text = f'{player_rsn}'
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

        # Draw text for rank, name, and total loot (colors added BY Smoke)
        if dynamic_colors:
            text_color = get_dynamic_color(bg_img)
        else:
            text_color = yellow
        draw.text((center_x, name_y), rsn_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)
        draw.text((rank_mid_x, rank_y), rank_num_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)
        draw.text((center_q_x, quant_y), gp_text, font=pet_font, fill=text_color, stroke_width=1, stroke_fill=black)

        # Update Y position for the next player
        if not first_name:
            name_y += 22
        else:
            name_y += 22
            first_name = False

    return bg_img

async def draw_drops_on_image(bg_img, draw, group_items, group_id, *, dynamic_colors=False, use_gp=False):
    """
    Draws the items on the image based on the quantities provided in group_items.
    
    :param bg_img: The background image to draw on.
    :param draw: The ImageDraw object to draw with.
    :param group_items: Dictionary of item_id and corresponding quantities/values.
    :param group_id: The group ID to determine specific placement rules if needed.
    :return: Updated background image with item images and quantities.
    """
    locations = {}
    small_font = ImageFont.truetype(rs_font_path, 16)
    amt_font = ImageFont.truetype(rs_font_path, 18)

    # Load item positions from the CSV file
    with open("data/item-mapping.csv", 'r') as csvfile:
        reader = csv.DictReader(csvfile)
        for i, row in enumerate(reader):
            locations[i] = row

    # Sort items by value and limit to top 32
    sorted_items = sorted(group_items.items(), key=lambda x: int(x[1]) if isinstance(x[1], int) else int(x[1].split(',')[1]), reverse=True)[:32]

    for i, (item_id, totals) in enumerate(sorted_items):
        try:
            quantity, total_value = map(int, totals.split(','))
        except ValueError as e:
            #print(f"Error processing item {item_id}: {e}")
            #print(f"Raw data: {totals}")
            continue  # Skip this item and move to the next one

        # print("Item:", sorted_items[i])
        # Get the item's position from the CSV file
        current_pos_x = int(locations[i]['x'])
        current_pos_y = int(locations[i]['y'])
        img_coords = (current_pos_x - 5, current_pos_y - 12)

        # Load the item image based on the item_id ( Coin dynamic loading based on value added BY Smoke)
        if int(item_id) == 995:
            coin_img_id = get_coin_image_id(quantity)
            item_img = await load_image_from_id(coin_img_id)
        else:
            item_img = await load_image_from_id(int(item_id))
        if not item_img:
            continue  # Skip if no image found

        # Resize and paste the item image onto the background
        scale_factor = 1.3
        new_width = round(item_img.width * scale_factor)
        new_height = round(item_img.height * scale_factor)
        item_img_resized = item_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        fixed_img = center_image(item_img_resized, 75, 60)
        bg_img.paste(fixed_img, img_coords, fixed_img)

        value_str = format_number(total_value)
        quantity_str = format_number(quantity)
        ctr_x = current_pos_x + 1
        ctr_y = current_pos_y - 10
        
        if dynamic_colors:
            text_color = get_dynamic_color(bg_img)
        else:
            text_color = yellow
        if use_gp:
            value_text_color = get_value_color(total_value)
        else:
            value_text_color = text_color
        # For coins, since the amount is redundant (equal to the value), display only the value. (added BY Smoke)
        if int(item_id) == 995:
            # Use the value-based color instead of the overall dynamic color.
            draw.text((ctr_x, ctr_y + 47), value_str, font=small_font, fill=value_text_color, stroke_width=1, stroke_fill=black)
        else:
            draw.text((ctr_x, ctr_y + 47), value_str, font=small_font, fill=value_text_color, stroke_width=1, stroke_fill=black)
            draw.text((ctr_x, ctr_y + 4), quantity_str, font=amt_font, fill=text_color, stroke_width=1, stroke_fill=black)

    return bg_img


async def draw_recent_drops(bg_img, draw, recent_drops, min_value, *, dynamic_colors, use_gp):
    """
    Draw recent drops on the image, filtering based on a minimum value.
    
    :param bg_img: Background image to draw on.
    :param draw: ImageDraw object to draw elements.
    :param recent_drops: List of recent drops to process.
    :param min_value: The minimum value of drops to be displayed.
    """
    # print("Recent drops:", recent_drops)
    try:
        min_value = int(min_value)
    except TypeError:
        min_value = 2500000
    # Filter the drops based on their value, keeping only those above the specified min_value
    filtered_recents = [drop for drop in recent_drops if drop['value'] >= min_value]
    
    # Sort drops by date in descending order and limit to the most recent 12 drops
    sorted_recents = sorted(filtered_recents, key=lambda x: x['date_added'], reverse=True)[:12]
    
    small_font = ImageFont.truetype(rs_font_path, 18)
    recent_locations = {}
    
    # Load locations for placing recent items on the board
    with open("data/recent-mapping.csv", 'r') as csvfile:
        reader = csv.DictReader(csvfile)
        for i, row in enumerate(reader):
            recent_locations[i] = row
    
    # Loop through the sorted recent drops and display them
    user_names = {}
    for i, data in enumerate(sorted_recents):
        if "drop_id" not in data:
            continue
        drop = session.query(Drop).filter(Drop.drop_id == data["drop_id"]).first()
        if not drop:
            continue
        player_id = drop.player_id
        player = session.query(Player).filter(Player.player_id == player_id).first()
        if not player:
            user_id = "Unknown"
            user_names[user_id] = "Unknown"
        else:
            user_id = player.player_id
            user_names[user_id] = player.player_name
        # Check if user_id is already cached in the user_names dictionary
        
        username = user_names[user_id]
        date_string = data["date_added"]
        try:
            # Try with microseconds
            date_obj = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%f')
        except ValueError:
            try:
                # Try ISO format without microseconds
                date_obj = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S')
            except ValueError:
                # Fallback to without microseconds and with space
                date_obj = datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
        
        # Get the item image based on the item name or ID (Dynamic coins id based on value added BY Smoke)
        item_id = data["item_id"]
        if int(item_id) == 995:
            try:
                coin_quantity = int(data["value"])
            except Exception:
                coin_quantity = 1
            coin_img_id = get_coin_image_id(coin_quantity)
            item_img = await load_image_from_id(coin_img_id)
        else:
            item_img = await load_image_from_id(item_id)
        if not item_img:
            continue
        
        # Get the x, y coordinates for the item based on recent_locations
        current_pos_x = int(recent_locations[i]['x'])
        current_pos_y = int(recent_locations[i]['y'])
        
        # Resize and paste the item image onto the background
        scale_factor = 1.3
        new_width = round(item_img.width * scale_factor)
        new_height = round(item_img.height * scale_factor)
        item_img_resized = item_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        fixed_item_img = center_image(item_img_resized, 75, 60)
        img_coords = (current_pos_x - 5, current_pos_y - 12)
        bg_img.paste(fixed_item_img, img_coords, fixed_item_img)
        
        # Draw text for username and time since the drop
        center_x = (current_pos_x + 1)
        center_y = (current_pos_y - 10)
        current_time = datetime.now()
        time_since = current_time - date_obj
        days, hours, minutes = time_since.days, time_since.seconds // 3600, (time_since.seconds // 60) % 60
        
        if days > 0:
            time_since_disp = f"({days}d {hours}h)"
        elif hours > 0:
            time_since_disp = f"({hours}h {minutes}m)"
        else:
            time_since_disp = f"({minutes}m)"
            
        # coloring added BY Smoke
        if dynamic_colors:
            text_color = get_dynamic_color(bg_img)
        else:
            text_color = yellow
        draw.text((center_x + 5, center_y), username, font=small_font, fill=text_color, stroke_width=1, stroke_fill=black)
        draw.text((current_pos_x, current_pos_y + 35), time_since_disp, font=small_font, fill=text_color, stroke_width=1, stroke_fill=black)
    return bg_img

def center_image(image, width, height):
    # Create a new image with the desired dimensions and a transparent background
    centered_image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    # Calculate the position where the original image should be pasted to be centered
    paste_x = (width - image.width) // 2
    paste_y = (height - image.height) // 2
    # Paste the original image onto the new image at the calculated position
    centered_image.paste(image, (paste_x, paste_y))
    return centered_image


def save_image(image, server_id, partition):
    """
    Save the generated lootboard image
    
    Args:
        image: The PIL Image object to save
        server_id: The group/server ID
        partition: The partition string (either YYYYMM or YYYY-MM-DD format)
    """
    # Create directory if it doesn't exist
    os.makedirs(f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb", exist_ok=True)
    
    # Determine if this is a daily partition
    is_daily = '-' in str(partition)
    
    if is_daily:
        # For daily partitions, use the date directly (YYYY-MM-DD)
        # Save as daily_YYYYMMDD.png
        formatted_date = partition.replace('-', '')
        file_path = f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb/daily_{formatted_date}.png"
        image.save(file_path)
        
        # Check if this is today's date
        current_date = datetime.now().strftime('%Y-%m-%d')
        if partition == current_date:
            # Also save as the default lootboard.png if it's today
            image.save(f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb/lootboard.png")
        
        return file_path
    else:
        # For monthly partitions, use the existing format (YYYYMM)
        current_date = datetime.now()
        today_ydmpart = int(current_date.strftime('%d%m%Y'))
        
        # Save with the date format
        file_path = f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb/{partition}.png"
        image.save(file_path)
        
        # Also save as today's date format if it's the current month
        current_month_partition = current_date.year * 100 + current_date.month
        if int(partition) == current_month_partition:
            today_path = f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb/{today_ydmpart}.png"
            image.save(today_path)
            # And save as the default lootboard.png
            image.save(f"/store/droptracker/disc/static/assets/img/clans/{server_id}/lb/lootboard.png")
        
        return file_path


async def load_image_from_id(item_id):
    if item_id == "None" or item_id is None or not isinstance(item_id, int):
        return None
    file_path = f"/store/droptracker/disc/static/assets/img/itemdb/{item_id}.png"
    item = session.query(ItemList).filter(ItemList.item_id == item_id).first()
    item_name = item.item_name
    if item.stackable:
        all_items = session.query(ItemList).filter(ItemList.item_name == item_name).all()
        target_item_id = [max(item.stacked, item.item_id) for item in all_items]
        item_id = target_item_id
    if not os.path.exists(file_path):
        await load_rl_cache_img(item_id)
    loop = asyncio.get_event_loop()
    try:
        # Run the blocking Image.open operation in a thread pool
        image = await loop.run_in_executor(None, Image.open, file_path)
        return image
    except Exception as e:
        print(f"The following file path: {file_path} produced an error: {e}")
        return None


async def load_rl_cache_img(item_id):
    url = f"https://static.runelite.net/cache/item/icon/{item_id}.png"
    try:
        ## save it here
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                # Ensure the request was successful
                if response.status != 200:
                    print(f"Failed to fetch image for item ID {item_id}. HTTP status: {response.status}")
                    return None

                # Read the response content
                image_data = await response.read()

                # Load the image data into a PIL Image object
                image = Image.open(BytesIO(image_data))
                file_path = f"/store/droptracker/disc/static/assets/img/itemdb/{item_id}.png"
                # print("Saving")
                image.save(file_path, "PNG")
                return image

    except Exception as e:
        print("Unable to load the item.")
    finally:
        await aiohttp.ClientSession().close()


def get_hourly_partitions_from_day(year, month, day):
    ## A partition for a day would contain all 24 hours:
    ## Using March 27th, 2025 as an example:
    ## 2025032700 - 2025032723
    ## And also include the last 59 minutes of the day:
    ## 202503272301 - 202503272359
    partitions = []
    for hour in range(24):
        partitions.append(f"{year}{month}{day}{hour:02d}")
        if hour == 23:  
            for minute in range(60):
                if minute == 0:
                    continue
                partitions.append(f"{year}{month}{day}{hour:02d}{minute:02d}")
    return partitions


async def generate_timeframe_board(bot: interactions.Client, group_id: int = 0, wom_group_id: int = 0, 
                                  start_time: datetime = None, end_time: datetime = None, npc_id: int = None):
    """
    Generate a loot board for a specific timeframe and optionally for a specific NPC.
    
    :param bot: Instance of the interactions.Client bot object
    :param group_id: DropTracker GroupID. 0 expects a wom_group_id
    :param wom_group_id: WiseOldMan groupID. 0 expects a group_id
    :param start_time: Start datetime for the timeframe (inclusive)
    :param end_time: End datetime for the timeframe (inclusive)
    :param npc_id: Optional NPC ID to filter drops by
    :return: Path to the generated image
    """
    # Set default times if not provided
    if group_id == 1:
        group_id = 2
    if start_time is None:
        # Default to start of current day
        start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
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
    
    # Get group information
    group = None
    if group_id != 0:  # We prioritize group_id here
        group = session.query(Group).filter(Group.group_id == group_id).first()
    elif wom_group_id != 0:
        group = session.query(Group).filter(Group.wom_id == wom_group_id).first()
    if (group_id != 0 or wom_group_id != 0) and not group:
        print("Cannot generate a lootboard, no group data was properly parsed..")
        return None
    elif (group_id == 0 and wom_group_id == 0):
        group_id = 1
    else:
        group_id = group.group_id
    
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
    
    # Get player IDs for the group
    if group_id != 2:
        if wom_group_id == 0 and group:
            wom_group_id = group.wom_id
        
        if wom_group_id != 0:
            player_wom_ids = await fetch_group_members(wom_group_id)
        else:
            player_wom_ids = [player[0] for player in session.query(Player.wom_id).all()]
    else:
        player_wom_ids = [p.wom_id for p in session.query(Player.wom_id).all()]
    
    player_ids = await associate_player_ids(player_wom_ids)
    
    # Generate time partitions to query
    time_partitions = generate_time_partitions(start_time, end_time, granularity)
    print("Got", len(time_partitions), "time partitions")
    # Get the drops, recent drops, and total loot for the group across all partitions
    group_items, player_totals, recent_drops, total_loot = await get_drops_for_timeframe(
        player_ids, time_partitions, granularity, npc_id
    )
    
    # Draw elements on the background image
    bg_img = await draw_drops_on_image(bg_img, draw, group_items, group_id, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Create a timeframe string for the header
    if npc_id:
        npc = session.query(NpcList).filter(NpcList.npc_id == npc_id).first()
        npc_name = npc.npc_name if npc else f"Unknown NPC ({npc_id})"
        timeframe_str = f"{start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')} - {npc_name}"
    else:
        timeframe_str = f"{start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')}"
    
    # Draw headers with custom timeframe string
    bg_img = await draw_headers(bot, group_id, total_loot, bg_img, draw, timeframe_str, 
                               dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Draw recent drops and leaderboard
    bg_img = await draw_recent_drops(bg_img, draw, recent_drops, min_value=minimum_value, 
                                    dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    bg_img = await draw_leaderboard(bg_img, draw, player_totals, dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    
    # Save the image with a custom filename
    timeframe_id = f"{start_time.strftime('%Y%m%d%H%M')}-{end_time.strftime('%Y%m%d%H%M')}"
    if npc_id:
        timeframe_id += f"-npc{npc_id}"
    
    image_path = save_image(bg_img, group_id, timeframe_id)
    return image_path

def generate_time_partitions(start_time, end_time, granularity):
    """
    Generate a list of time partition strings between start_time and end_time
    based on the specified granularity.
    """
    partitions = []
    current_time = start_time
    
    if granularity == "monthly":
        # Generate monthly partitions (YYYYMM)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next month
            if current_time.month == 12:
                current_time = current_time.replace(year=current_time.year + 1, month=1)
            else:
                current_time = current_time.replace(month=current_time.month + 1)
    
    elif granularity == "daily":
        # Generate daily partitions (YYYYMMDD)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next day
            current_time = current_time + timedelta(days=1)
    
    elif granularity == "hourly":
        # Generate hourly partitions (YYYYMMDDHH)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d%H')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next hour
            current_time = current_time + timedelta(hours=1)
    
    else:  # minute
        # Generate minute partitions (YYYYMMDDHHMM)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d%H%M')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next minute
            current_time = current_time + timedelta(minutes=1)
    
    return partitions

async def get_drops_for_timeframe(player_ids, time_partitions, granularity, npc_id=None):
    """
    Returns the drops stored in redis cache for the specific list of player_ids
    across multiple time partitions, optionally filtered by NPC.
    """
    group_items = {}
    recent_drops = []
    total_loot = 0
    player_totals = {}
    
    # Determine Redis key prefix based on granularity
    if granularity == "monthly":
        prefix = ""  # Monthly has no prefix
    elif granularity == "daily":
        prefix = "daily"
    elif granularity == "hourly":
        prefix = "hourly"
    else:
        prefix = "minute"
    
    async def process_player(player_id):
        nonlocal total_loot
        player_total = 0
        for partition in time_partitions:
            # Construct Redis keys based on granularity
            if prefix:
                total_items_key = f"player:{player_id}:{prefix}:{partition}:items"
                recent_items_key = f"player:{player_id}:{prefix}:{partition}:recent_items"
                loot_key = f"player:{player_id}:{prefix}:{partition}:total_loot"
                
                # If filtering by NPC, use the NPC-specific keys
                if npc_id:
                    total_items_key = f"player:{player_id}:{prefix}:{partition}:npc_items:{npc_id}"
            else:
                # Monthly format
                total_items_key = f"player:{player_id}:{partition}:total_items"
                recent_items_key = f"player:{player_id}:{partition}:recent_items"
                loot_key = f"player:{player_id}:{partition}:total_loot"
                
                # If filtering by NPC, we need to check NPC totals
                if npc_id:
                    npc_key = f"player:{player_id}:{partition}:npc_totals"
                    npc_value = redis_client.client.hget(npc_key, str(npc_id))
                    if npc_value:
                        player_total += int(npc_value.decode('utf-8'))
                    continue  # Skip item processing for monthly NPC filtering
            
            # Get total items
            total_items = redis_client.client.hgetall(total_items_key)
            
            for key, value in total_items.items():
                key = key.decode('utf-8')
                value = value.decode('utf-8')
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
            
            # Get player total for this partition
            if not npc_id or prefix:  # Skip for monthly NPC filtering
                partition_total = redis_client.client.get(loot_key)
                if partition_total:
                    partition_total = int(partition_total.decode('utf-8'))
                    if npc_id and prefix:
                        # For time-based NPC filtering, we need to check if this total is for our NPC
                        npc_key = f"player:{player_id}:{prefix}:{partition}:npcs"
                        npc_value = redis_client.client.hget(npc_key, str(npc_id))
                        if npc_value:
                            player_total += int(npc_value.decode('utf-8'))
                    else:
                        player_total += partition_total
            
            # Get recent items
            if not npc_id:  # Only process recent items if not filtering by NPC
                recent_items_raw = redis_client.client.lrange(recent_items_key, 0, -1)
                for item in recent_items_raw:
                    item_data = json.loads(item.decode('utf-8'))
                    # Add to recent drops if not already present
                    if not any(drop['drop_id'] == item_data['drop_id'] for drop in recent_drops):
                        recent_drops.append(item_data)
        if player_total > 0:
            print(f"Player {player_id} total:", player_total)
        return player_id, player_total, []  # Empty list for recent items as we process them separately
    
    # Process all players concurrently
    tasks = [process_player(player_id) for player_id in player_ids]
    results = await asyncio.gather(*tasks)
    
    # Process results
    for player_id, player_total, _ in results:
        if player_total > 0:
            player_totals[player_id] = player_total
            total_loot += player_total
    
    # Sort recent drops by date (newest first)
    recent_drops.sort(key=lambda x: x['date_added'], reverse=True)
    
    # Limit to most recent 10 drops
    recent_drops = recent_drops[:10]
    
    return group_items, player_totals, recent_drops, total_loot
