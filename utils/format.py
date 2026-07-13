from io import BytesIO
import json
import os
import re
import time
import unicodedata
from datetime import datetime
import aiohttp
import interactions
from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from PIL import Image, ImageFont, ImageDraw
from db import NpcList, session, models

DOCS_FOLDER = os.path.join(os.getcwd(), 'templates/docs')

def format_time_since_update(datetime_object):
    """ 
        Returns a discord-formatted timestamp like '15 seconds ago' or 'in 3 days',
        which is non-timezone-specific.
    """
    # Convert the DateTime object to a Unix timestamp
    if datetime_object:
        unix_timestamp = int(datetime_object.timestamp())
    else:
        unix_timestamp = int(time.time())  # Default to current time if date_updated is None

    # Format the timestamp for Discord
    return f"<t:{unix_timestamp}:R>"

def format_number(number):
    if not number:
        return "0"
    try:
        number = number.decode('utf-8')
    except:
        pass
    try:
        number = int(float(number))
    except:
        number = int(number)
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.3f}B"
    elif number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    elif number >= 1_000:
        return f"{number / 1_000:.2f}K"
    else:
        return f"{number:,}"


def get_current_partition() -> int:
    """
        Returns the naming scheme for a partition of drops
        Based on the current month
    """
    now = datetime.now()
    return now.year * 100 + now.month

def normalize_npc_name(npc_name: str):
    return npc_name.replace(" ", "_").strip()

def normalize_player_display_equivalence(name: str) -> str:
    """
    Normalize a player name for equivalence comparison where the external
    library replaces hyphens/underscores with spaces. This keeps alphanumerics
    and converts '-', '_' to a single space, then collapses whitespace and
    lowercases for robust comparison.
    """
    if name is None:
        return ""
    # Replace '-' and '_' with spaces, collapse whitespace, and lowercase
    name = str(name).replace('-', ' ').replace('_', ' ')
    name = " ".join(name.split())
    return name.lower()


def normalize_claim_rsn_input(name: str) -> str:
    """
    Normalize an RSN from Discord or other UI before DB lookup: NFKC, common
    unicode space characters to ASCII space, collapse runs of whitespace, strip.
    """
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name).strip())
    s = re.sub(r"[\u00a0\u2000-\u200b\u202f\u205f\u3000]", " ", s)
    s = " ".join(s.split())
    return s


def get_player_by_claim_rsn(sess, player_model, rsn: str):
    """
    Resolve a Player for /claim-rsn using equality on lower(trim(name)) so we
    do not rely on SQL LIKE (wildcards) and we tolerate Discord unicode quirks.
    Also tries OSRS space vs underscore display equivalence (see point_awards).
    """
    norm = normalize_claim_rsn_input(rsn)
    if not norm:
        return None
    nm = norm.lower()
    p = sess.query(player_model).filter(
        func.lower(func.trim(player_model.player_name)) == nm
    ).first()
    if p:
        return p
    if " " in norm or "_" in norm:
        alt = norm.replace(" ", "_") if " " in norm else norm.replace("_", " ")
        alt_l = alt.lower()
        p = sess.query(player_model).filter(
            func.lower(func.trim(player_model.player_name)) == alt_l
        ).first()
        if p:
            return p
    return sess.query(player_model).filter(player_model.player_name.ilike(norm)).first()


def get_true_boss_name(npc_name: str):
    """
        Returns the name of the NPC we are storing in the database for a given npc name passed;
        generally coming from an adventure log message.
    """
    if npc_name == "Theatre of Blood Hard Mode":
        npc_name = "Theatre of Blood: Hard Mode"
    npc = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
    if npc:
        print("Found an exact match for", npc_name, "in the database:", npc.npc_name, npc.npc_id)
        return npc.npc_name, npc.npc_id
    else:
        # Normalized match (suggestion #50): same match key ⇒ same boss
        # (spelling, punctuation, "The " article and alias variants like
        # Crystalline Hunllef → The Gauntlet), preferring the id that already
        # has tracked data — instead of the ilike guess below.
        from sqlalchemy import bindparam
        from sqlalchemy import text as _text

        from utils.npc_names import npc_match_variants, npc_slug_sql_expr

        variants = npc_match_variants(npc_name)
        if variants:
            row = session.execute(
                _text(
                    f"SELECT n.npc_id, n.npc_name, "
                    f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
                    f"              WHERE t.npc_id = n.npc_id) AS tracked "
                    f"FROM npc_list n WHERE {npc_slug_sql_expr('n.npc_name')} IN :variants "
                    f"ORDER BY tracked DESC, n.npc_id ASC LIMIT 1"
                ).bindparams(bindparam("variants", expanding=True)),
                {"variants": variants},
            ).first()
            if row:
                print("Found a normalized match for", npc_name, "in the database:", row[1], row[0])
                return row[1], int(row[0])
        ## Try to find the closest match in the database
        npc = session.query(NpcList).filter(NpcList.npc_name.ilike(f"%{npc_name}%")).first()
        if npc:
            print("Found a close match for", npc_name, "in the database:", npc.npc_name, npc.npc_id)
            return npc.npc_name, npc.npc_id
        else:
            print("No match found for", npc_name, "in the database")
            return "Unknown", None


async def get_command_id(bot: interactions.Client, command_name: str):
    """
        Attempts to return the Discord ID for the passed 
        command name based on the context of the bot being used,
        incase the client is changed which would result in new command IDs
    """
    try:
        commands = bot.application_commands
        if commands:
            for command in commands:
                cmd_name = command.get_localised_name("en")
                if cmd_name == command_name:
                    return command.cmd_id[0]
        return "`command not yet added`"
    except Exception as e:
        print("Couldn't retrieve the ID for the command")
        print("Exception:", e)


def get_extension_from_content_type(content_type):
    if content_type and '/' in content_type:
        # Map common content types to standard extensions
        content_type_lower = content_type.lower()
        if 'jpeg' in content_type_lower or content_type_lower == 'image/jpg':
            return 'jpg'
        elif 'png' in content_type_lower:
            return 'png'
        elif 'gif' in content_type_lower:
            return 'gif'
        elif 'webp' in content_type_lower:
            return 'webp'
        else:
            # Default case - extract after the slash but ensure it's a valid extension
            ext = content_type.split('/')[-1]
            # Remove any additional parameters (e.g., "jpeg; charset=utf-8")
            ext = ext.split(';')[0].strip()
            return ext if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else 'jpg'
    return 'jpg'  # Default to jpg if content type is not provided


async def get_npc_image_url(npc_name, npc_id):
    """
        Requires the EXACT npc name be passed, and may (oftentimes) fail due to different pathing on the RS wiki
    """
    os.makedirs('/store/droptracker/disc/static/assets/img/npcdb',exist_ok=True)
    base_url = "https://oldschool.runescape.wiki/images/thumb/TzKal-Zuk.png/280px-TzKal-Zuk.png"
    if not os.path.exists(f'/static/assets/img/npcdb/{npc_id}.png'):
        try:
            npc_name = normalize_npc_name(npc_name)
            url = f"https://oldschool.runescape.wiki/images/thumb/{npc_name}.png/280px-{npc_name}.png"
            ## save it here
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # Ensure the request was successful
                    if response.status != 200:
                        print(f"Failed to fetch image for npc {npc_name}. HTTP status: {response.status}")
                        return None

                    # Read the response content
                    image_data = await response.read()

                    # Load the image data into a PIL Image object
                    image = Image.open(BytesIO(image_data))
                    file_path = f"/store/droptracker/disc/static/assets/img/npcdb/{npc_id}.png"
                    image.save(file_path, "PNG")
                    return f"https://www.droptracker.io/img/npcdb/{npc_id}.png"

        except Exception as e:
            print("We were unable to load the npc image:", e)
        finally:
            await aiohttp.ClientSession().close()
    else:
        return f"https://www.droptracker.io/img/npcdb/{npc_id}.png"
            
    

def replace_placeholders(embed: interactions.Embed, value_dict: dict, global_server: bool = False):

    # Replace placeholders in the embed title
    #print("replace_placeholders called with value_dict:", value_dict)
    if embed.title:
        if "{npc_name}" in embed.title:
            embed.title = replace_placeholders_in_text(embed.title, value_dict)
            formatted_npc_name = value_dict.get('{npc_name}', '').replace(" ", "_")
            wiki_url = f"https://oldschool.runescape.wiki/w/{formatted_npc_name}"
            embed.url = wiki_url
        elif "{item_name}" in embed.title:
            embed.title = replace_placeholders_in_text(embed.title, value_dict)
            formatted_item_name = value_dict.get('{item_name}', '').replace(" ", "_")
            wiki_url = f"https://oldschool.runescape.wiki/w/{formatted_item_name}"
            embed.url = wiki_url
        else:
            embed.title = replace_placeholders_in_text(embed.title, value_dict)
    
    # Replace placeholders in the embed description
    if embed.description:
        if "{kc_received}" in embed.description:
            if (value_dict.get("{kc_received}", None) == "n/a" or value_dict.get("{npc_name}", None) == "unknown"):
                embed.description = None
            else:
                embed.description = replace_placeholders_in_text(embed.description, value_dict)
        else:
            embed.description = replace_placeholders_in_text(embed.description, value_dict)
    
    # Replace placeholders in the embed footer
    if embed.footer and embed.footer.text:
        embed.footer.text = replace_placeholders_in_text(embed.footer.text, value_dict)
    
    # Replace placeholders in each field's name and value.
    # Build a new field list to avoid index/pop skipping issues.
    if embed.fields:
        kept_fields = []
        group_point_placeholders = (
            "{group_points_awarded}",
            "{group_points_receiver_total}",
            "{group_points_member_count}",
            "{group_points_members_awarded}",
        )
        for field in embed.fields:
            field_name = field.name or ""
            field_value = field.value or ""

            if global_server and "Group" in field_name:
                continue

            if field_name:
                field_name = replace_placeholders_in_text(field_name, value_dict)

            if field_name == "Source:" and value_dict.get("{kill_count}", None) is None:
                continue

            if field_value:
                if field_value == "{team_size}":
                    if value_dict.get("{team_size}", None) != "Solo":
                        original_value = value_dict["{team_size}"]
                        value_dict["{team_size}"] = f"{original_value} players"
                field_value = replace_placeholders_in_text(field_value, value_dict)

            # If group-point placeholders are still present after replacement,
            # data was unavailable for this event; suppress that field.
            combined = f"{field_name} {field_value}"
            if any(placeholder in combined for placeholder in group_point_placeholders):
                continue

            if str(field_value).strip() == "":
                continue

            field.name = field_name
            field.value = field_value
            kept_fields.append(field)
        embed.fields = kept_fields
    
    # Replace placeholders in the embed's thumbnail URL
    if embed.thumbnail and embed.thumbnail.url:
        if "{item_id}" in embed.thumbnail.url:
            item_id = value_dict.get("{item_id}", None)
            if item_id: 
                embed.thumbnail.url = f"https://static.runelite.net/cache/item/icon/{item_id}.png"
        else:
            embed.thumbnail.url = replace_placeholders_in_text(embed.thumbnail.url, value_dict)
    
    # Replace placeholders in the embed's image URL
    if embed.image and embed.image.url:
        embed.image = None

        # embed.image.url = replace_placeholders_in_text(embed.image.url, value_dict)
    #print("Placeholder replacement complete.")
    return embed

def replace_placeholders_in_text(text, value_dict):
    for placeholder, value in value_dict.items():
        try:
            text = text.replace(placeholder, str(value))
        except Exception as e:
            text = text
            print("Couldn't replace placeholders in", text, f"using placeholder/value {placeholder}/{value}")
    
    return text

def convert_to_ms(kill_time: str):
    """
    Converts an incoming time from the RuneLite plugin 
    (i.e, 1:33.00 or 00:50.40) to the total milliseconds.
    """
    total_splits = kill_time.count(":")
    
    if total_splits == 1:
        mins, seconds = kill_time.split(":")
        mins = int(mins)
        seconds, ticks = seconds.split(".") if "." in seconds else (seconds, 0)
        seconds = int(seconds)
        ticks = int(ticks)
        total_seconds = seconds + (mins * 60)
        # ticks are in hundredths of a second when present, so multiply by 10 to convert to ms
        ms = (ticks * 10) + (total_seconds * 1000)
        return ms
    
    elif total_splits == 2:
        hours, mins, seconds = kill_time.split(":")
        hours = int(hours)
        mins = int(mins)
        seconds, ticks = seconds.split(".") if "." in seconds else (seconds, 0)
        seconds = int(seconds)
        ticks = int(ticks)
        total_seconds = seconds + (mins * 60) + (hours * 3600)
        ms = (ticks * 10) + (total_seconds * 1000)
        return ms

    return None  # in case of invalid input
    
def convert_from_ms(ms: int):
    """
    Converts a time from total milliseconds to a human-readable format
    (HH:MM:SS.t) where t is tenths of a second.
    """
    # Calculate total hours, minutes, and seconds
    hours = ms // (3600 * 1000)
    ms %= (3600 * 1000)

    minutes = ms // (60 * 1000)
    ms %= (60 * 1000)

    seconds = ms // 1000
    ms %= 1000

    # Remaining ms are tenths of a second
    ticks = ms // 100  # tenths of a second

    # Format based on whether we have hours or just minutes
    if hours > 0:
        return f"{hours}:{minutes:02}:{seconds:02}.{ticks}"
    else:
        return f"{minutes}:{seconds:02}.{ticks}"
    
def parse_authed_users(config):
    authed_users = config.get('authed_users', '[]')  # Default to an empty list if not set
    if isinstance(authed_users, str):
        # Replace single quotes with double quotes to make it valid JSON
        cleaned_authed_users = authed_users.replace("'", '"')
        try:
            authed_users = json.loads(cleaned_authed_users)
        except json.JSONDecodeError:
            # If parsing fails, fallback to an empty list
            authed_users = []
    elif not isinstance(authed_users, list):
        authed_users = []  # Ensure it's a list

    config['authed_users'] = authed_users
    return config

def parse_redis_data(redis_data):
    parsed_data = {}
    for key, value in redis_data.items():
        # Decode the key from bytes to a string
        key = key.decode('utf-8')

        # Try to decode the value based on its format
        try:
            value = value.decode('utf-8')
            
            # If the value is a JSON string (like a list), parse it as JSON
            if value.startswith('[') or value.startswith('{'):
                value = json.loads(value)
            
            # Convert "boolean-like" strings to actual booleans
            elif value in ['true', 'false']:
                value = value == 'true'
            
            # Convert "integer-like" strings to integers
            elif value.isdigit():
                value = int(value)
            
        except Exception as e:
            pass
            # print(f"Error decoding value for {key}: {e}")
        
        # Add the properly decoded key-value pair to the parsed data
        parsed_data[key] = value

    return parsed_data


def parse_stored_sheet(sheet_id_or_url):
    """
    Accepts either a Google Sheet URL or a Sheet ID and returns the sheet ID.
    
    :param sheet_id_or_url: A string that can either be a full Google Sheets URL or just the sheet ID.
    :return: The Google Sheet ID as a string.
    """
    # Regex to match the Google Sheet URL format and capture the ID
    url_pattern = r"https://docs.google.com/spreadsheets/d/([a-zA-Z0-9-_]+)"
    
    match = re.match(url_pattern, sheet_id_or_url)
    
    if match:
        # If it's a URL, extract the ID from the matched group
        return match.group(1)
    else:
        # If it's not a URL, assume it's already a sheet ID
        return sheet_id_or_url
    
def human_readable_time_difference(timestamp_str):
    # Parse the timestamp string into a datetime object
    timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    
    # Get the current time
    now = datetime.now()
    
    # Calculate the difference between now and the timestamp
    delta = relativedelta(now, timestamp)
    
    # Create a human-readable format for the time difference
    if delta.years > 0:
        return f"{delta.years} years ago" if delta.years == 1 else f"{delta.years} years ago"
    elif delta.months > 0:
        return f"{delta.months} months ago" if delta.months == 1 else f"{delta.months} months ago"
    elif delta.days > 0:
        return f"{delta.days} days ago" if delta.days == 1 else f"{delta.days} days ago"
    elif delta.hours > 0:
        return f"{delta.hours} hours ago" if delta.hours == 1 else f"{delta.hours} hours ago"
    elif delta.minutes > 0:
        return f"{delta.minutes} minutes ago" if delta.minutes == 1 else f"{delta.minutes} minutes ago"
    else:
        return "just now"


def get_sorted_doc_files():
    doc_files = []
    ## show these files first
    priority_files = ['getting-started.md', 'runelite.md']
    for root, dirs, files in os.walk(DOCS_FOLDER):
        if root == DOCS_FOLDER:
            for file in files:
                if file.endswith('.md'):
                    doc_files.append(file)
        else:
            break
    # Extract priority files
    sorted_doc_files = [file for file in priority_files if file in doc_files]

    # Add the rest of the files (excluding priority files)
    sorted_doc_files += [file for file in doc_files if file.lower() not in priority_files]
    
    return sorted_doc_files

