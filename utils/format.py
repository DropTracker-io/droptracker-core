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


def prefer_display_casing(current: str, candidate: str):
    """Pick the better-capitalised spelling of one RSN, or None to keep `current`.

    WOM's `username` is documented as "always lowercase", and it is what
    check_user_by_username used to hand back, so more than half our rows were
    created with the capitalisation flattened out of them. Both WOM's
    `display_name` and the RSN the plugin reads off the game client carry the
    real casing, and this decides when one of those may overwrite what we hold.

    `candidate` wins only when it is the same name letter-for-letter *and*
    separator-for-separator — differing purely in case — and `current` has no
    capitalisation while `candidate` does. That makes the swap invisible to
    every lookup, since all of them compare through
    normalize_player_display_equivalence() or LOWER(), and it cannot rewrite a
    name into a different identity.

    Deliberately one-way: a name that already carries capitals is never
    reworded, so a lowercase source can't flatten a good name back out and two
    disagreeing sources can't write over each other on every submission.
    """
    if not current or not candidate:
        return None
    current = str(current)
    candidate = str(candidate)
    if current == candidate:
        return None
    # Same characters modulo case: guarantees only capitalisation changes.
    if current.casefold() != candidate.casefold():
        return None
    if current != current.lower():
        return None
    if candidate == candidate.lower():
        return None
    return candidate


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

        Runs on its OWN short-lived session. These are pure reads, but a read
        on the module-global scoped session autobegins a transaction that this
        function never ends — and its caller (adventure_log) runs inside the
        long-lived webhook consumer, whose only scoped-session cleanup is gated
        on the worker being idle and fires at most once a minute. Each lookup
        therefore pinned a pooled connection with an idle transaction.
    """
    from db.models.base import Session

    if npc_name == "Theatre of Blood Hard Mode":
        npc_name = "Theatre of Blood: Hard Mode"
    with Session() as session:
        return _lookup_boss_name(session, npc_name)


def _lookup_boss_name(session, npc_name: str):
    # Multi-boss encounters resolve to the encounter row BEFORE the exact-name
    # lookup: "Crystalline Hunllef" and "Corrupted Hunllef" have npc_list rows
    # of their own, so an exact match would store Gauntlet PBs on the boss id
    # while the loot path uses the activity id — the split PB boards the
    # adventure log kept re-creating.
    from utils.npc_names import canonical_encounter_name

    npc_name = canonical_encounter_name(npc_name)
    npc = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
    if npc:
        print("Found an exact match for", npc_name, "in the database:", npc.npc_name, npc.npc_id)
        return npc.npc_name, npc.npc_id
    else:
        # Normalized match (suggestion #50): same match key ⇒ same boss
        # (spelling, punctuation, "The " article and alias variants like
        # Crystalline Hunllef → The Gauntlet), preferring the encounter's own
        # spelling and then the id that already has tracked data — instead of
        # the prefix guess below.
        from sqlalchemy import bindparam
        from sqlalchemy import text as _text

        from utils.npc_names import (
            npc_match_variants,
            npc_primary_rank_sql_expr,
            npc_primary_variants,
            npc_slug_sql_expr,
        )

        variants = npc_match_variants(npc_name)
        if variants:
            row = session.execute(
                _text(
                    f"SELECT n.npc_id, n.npc_name, "
                    f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
                    f"              WHERE t.npc_id = n.npc_id) AS tracked "
                    f"FROM npc_list n WHERE {npc_slug_sql_expr('n.npc_name')} IN :variants "
                    f"ORDER BY {npc_primary_rank_sql_expr('n.npc_name')} ASC, "
                    f"         tracked DESC, n.npc_id ASC LIMIT 1"
                ).bindparams(
                    bindparam("variants", expanding=True),
                    bindparam("primary_variants", expanding=True),
                ),
                {"variants": variants, "primary_variants": npc_primary_variants(npc_name)},
            ).first()
            if row:
                print("Found a normalized match for", npc_name, "in the database:", row[1], row[0])
                return row[1], int(row[0])
        ## Try to find a more specific row that EXTENDS this name, e.g.
        ## "Doom of Mokhaiotl" → "Doom of Mokhaiotl (Level 3)". Anchored to the
        ## start on purpose: an unanchored "%name%" matched anything merely
        ## mentioning the boss, so "Wintertodt" resolved to "Reward cart
        ## (Wintertodt)" and "Hallowed Sepulchre" to "Coffin (Hallowed
        ## Sepulchre)" — storing kills against scenery instead of the boss.
        npc = session.query(NpcList).filter(NpcList.npc_name.ilike(f"{npc_name}%")).first()
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


NPC_IMG_DIR = '/store/droptracker/disc/static/assets/img/npcdb'
# All HOF thumbnails are contain-fit onto a square canvas of this size so
# wildly different wiki thumbnail aspect ratios (174px-688px tall, all at a
# fixed 280px width) don't render as inconsistently-sized boss icons.
NPC_IMG_CANONICAL_SIZE = (280, 280)


def normalize_npc_image(image: Image.Image) -> Image.Image:
    """Contain-fit `image` onto a transparent NPC_IMG_CANONICAL_SIZE canvas, centered."""
    image = image.convert("RGBA")
    fitted = image.copy()
    fitted.thumbnail(NPC_IMG_CANONICAL_SIZE, Image.LANCZOS)
    canvas = Image.new("RGBA", NPC_IMG_CANONICAL_SIZE, (0, 0, 0, 0))
    offset = (
        (NPC_IMG_CANONICAL_SIZE[0] - fitted.width) // 2,
        (NPC_IMG_CANONICAL_SIZE[1] - fitted.height) // 2,
    )
    canvas.paste(fitted, offset, fitted)
    return canvas


async def get_npc_image_url(npc_name, npc_id):
    """
        Requires the EXACT npc name be passed, and may (oftentimes) fail due to different pathing on the RS wiki.
        Downloads the wiki thumbnail (if not already cached on disk), normalizes it to
        NPC_IMG_CANONICAL_SIZE, and returns the CDN URL. Returns None if the image
        could not be fetched.
    """
    os.makedirs(NPC_IMG_DIR, exist_ok=True)
    file_path = f"{NPC_IMG_DIR}/{npc_id}.png"
    cdn_url = f"https://www.droptracker.io/img/npcdb/{npc_id}.png"
    if os.path.exists(file_path):
        return cdn_url
    try:
        normalized_name = normalize_npc_name(npc_name)
        url = f"https://oldschool.runescape.wiki/images/thumb/{normalized_name}.png/280px-{normalized_name}.png"
        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(url) as response:
                if response.status != 200:
                    print(f"Failed to fetch image for npc {npc_name}. HTTP status: {response.status}")
                    return None
                image_data = await response.read()
        image = Image.open(BytesIO(image_data))
        normalized_image = normalize_npc_image(image)
        normalized_image.save(file_path, "PNG")
        return cdn_url
    except Exception as e:
        print("We were unable to load the npc image:", e)
        return None


# Discord renders an embed *title* as plain text — masked links, bold, code
# ticks and the rest all show up with their markers intact. Descriptions and
# field values DO render markdown, so this flattening is title-only.
#
# The masked-link case is the one that bites: {player_name} resolves to
# `[Name](profile_url)` (utils/site_urls.player_link), which reads fine in a
# description but lands in a title as literal brackets and a raw URL.
_TITLE_MD_LINK = re.compile(r"\[([^\[\]]*)\]\(\s*<?[^)\s]*>?(?:\s+\"[^\"]*\")?\s*\)")

# Paired emphasis markers only, so a lone marker character survives. Single
# `_underscore_` is deliberately absent: OSRS display names legitimately
# contain underscores (the plugin submits `Beast_Owned`), and stripping them
# would corrupt names for the sake of an italic nobody writes in a title.
_TITLE_MD_MARKERS = (
    re.compile(r"\*\*\*(.+?)\*\*\*", re.S),
    re.compile(r"\*\*(.+?)\*\*", re.S),
    re.compile(r"\*(.+?)\*", re.S),
    re.compile(r"___(.+?)___", re.S),
    re.compile(r"__(.+?)__", re.S),
    re.compile(r"~~(.+?)~~", re.S),
    re.compile(r"`+([^`]+)`+"),
)


def strip_title_markdown(text):
    """Flatten Discord markdown that an embed title cannot render.

    ``[Name](url)`` becomes ``Name``; ``**bold**``, ``~~strike~~`` and
    `` `code` `` lose their markers. Everything else is left alone.
    """
    if not text:
        return text
    out = _TITLE_MD_LINK.sub(r"\1", str(text))
    for pattern in _TITLE_MD_MARKERS:
        out = pattern.sub(r"\1", out)
    return out


def _wiki_url(name) -> str:
    return f"https://oldschool.runescape.wiki/w/{str(name or '').replace(' ', '_')}"


_TITLE_WHITESPACE = re.compile(r"\s+")


def tidy_title(text):
    """Collapse the whitespace an empty placeholder leaves behind.

    ``{item_emoji} {item_name}`` resolves to ``" Abyssal whip"`` for an item
    with no glyph — and Discord renders that leading space. A title is one line
    by construction, so collapsing runs and trimming can only remove whitespace
    no template meant to keep. Applied after substitution, never before: the
    template itself is what the editor round-trips.
    """
    return _TITLE_WHITESPACE.sub(" ", str(text or "")).strip()


def replace_placeholders(embed: interactions.Embed, value_dict: dict, global_server: bool = False):

    # Replace placeholders in the embed title
    #print("replace_placeholders called with value_dict:", value_dict)
    if embed.title:
        # A stored template url (group_embeds.url) wins and may carry its own
        # placeholders. Only when it is blank do we fall back to the historical
        # behaviour: link the title at the wiki page for whichever of
        # {npc_name}/{item_name} the *template* mentioned.
        custom_url = embed.url
        title_has_npc = "{npc_name}" in embed.title
        title_has_item = "{item_name}" in embed.title

        embed.title = tidy_title(
            strip_title_markdown(replace_placeholders_in_text(embed.title, value_dict))
        )

        resolved_url = ""
        if custom_url:
            resolved_url = replace_placeholders_in_text(custom_url, value_dict).strip()
            # An unresolved placeholder leaves a non-URL behind; Discord rejects
            # the whole embed for a malformed url, so drop it rather than send it.
            if not resolved_url.lower().startswith(("http://", "https://")):
                resolved_url = ""

        if resolved_url:
            embed.url = resolved_url
        elif title_has_npc:
            embed.url = _wiki_url(value_dict.get("{npc_name}", ""))
        elif title_has_item:
            embed.url = _wiki_url(value_dict.get("{item_name}", ""))
        else:
            embed.url = None

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
                field_values = value_dict
                if field_value == "{team_size}":
                    team_size = value_dict.get("{team_size}", None)
                    # Suffix in a local copy: mutating value_dict would compound
                    # into "4 players players" on a second field that references
                    # {team_size}, and a template can carry one even when the
                    # caller supplies no team size at all.
                    if team_size is not None and team_size != "Solo":
                        field_values = {**value_dict, "{team_size}": f"{team_size} players"}
                field_value = replace_placeholders_in_text(field_value, field_values)

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

