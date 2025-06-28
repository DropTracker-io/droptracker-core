import aiofiles
import os
import re
from db.models import Player
import uuid

import aiohttp

async def download_image(sub_type: str,
                         player: Player,
                         file_data,
                         processed_data):
        base_dir = "/store/droptracker/disc/static/assets/img/user-upload/"
    
        base_url = "https://www.droptracker.io/img/user-upload/"
        directory_path = os.path.join(base_dir, str(player.wom_id), sub_type)
        sub_type = sub_type if sub_type != "npc" and sub_type != "other" else "drop"
        
        # Get the appropriate field name based on submission type
        if sub_type == "collection_log" or sub_type == "clog":
            path_component = processed_data.get('source', 'unknown')
        elif sub_type == "pb":
            path_component = processed_data.get('boss_name', 'unknown')
        else:
            path_component = processed_data.get('source', processed_data.get('npc_name', 'unknown'))
        
        url_path = f"{player.wom_id}/{sub_type}/{path_component}/"

        def sanitize_filename(filename):
            """Sanitize filename to remove/replace problematic characters"""
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            filename = re.sub(r'\s+', '_', filename)
            return filename.strip('. ')

        def generate_unique_filename(directory, base_name_with_ext):
            """Generate unique filename, handling files that already have extensions"""
            # Split the filename and extension
            if '.' in base_name_with_ext:
                base_name, ext = base_name_with_ext.rsplit('.', 1)
            else:
                base_name, ext = base_name_with_ext, 'jpg'
            
            counter = 1
            unique_file_name = f"{base_name}.{ext}"
            while os.path.exists(os.path.join(directory, unique_file_name)):
                unique_file_name = f"{base_name}_{counter}.{ext}"
                counter += 1
            return unique_file_name
        
        try:
            # Generate unique filename based on submission type
            if sub_type == "drop":
                source_name = processed_data.get("source", "unknown")
                item_name = processed_data.get("item", "unknown")
                directory_path = os.path.join(directory_path, source_name)
                if item_name and source_name:
                    filename = f"{source_name}_{item_name}.jpg"
                else:
                    filename = f"{item_name}.jpg"
                filename = generate_unique_filename(directory_path, filename)
            elif sub_type == "pb":
                boss_name = processed_data.get("boss_name", processed_data.get("npc_name", "unknown"))
                team_size = processed_data.get("team_size", "solo")
                time_value = processed_data.get("time", "unknown")
                directory_path = os.path.join(directory_path, boss_name)
                filename = f"{boss_name}_{team_size}_{time_value}.jpg"
                filename = generate_unique_filename(directory_path, filename)
            elif sub_type == "clog" or sub_type == "collection_log":
                source_name = processed_data.get("source", "unknown")
                item_name = processed_data.get("item", "unknown")
                directory_path = os.path.join(directory_path, source_name)
                filename = f"{item_name}.jpg"
                filename = generate_unique_filename(directory_path, filename)
            elif sub_type == "ca" or sub_type == "combat_achievement":
                task_name = processed_data.get("task_name", processed_data.get("task", "unknown"))
                task_tier = processed_data.get("task_tier", processed_data.get("tier", "unknown"))
                directory_path = os.path.join(directory_path, task_tier)
                filename = f"{task_name}_{task_tier}.jpg"
                filename = generate_unique_filename(directory_path, filename)
            else:
                # Default fallback
                filename = f"submission_{uuid.uuid4()}.jpg"
            
            os.makedirs(directory_path, exist_ok=True)
            filepath = os.path.join(directory_path, filename)
            
            # Save the file
            await file_data.save(filepath)
            
            # Add the filepath to the processed data
            processed_data["image_path"] = f"{base_url}{url_path}{filename}"
            
            print(f"Saved image to {filepath}")
            return filepath
        except Exception as e:
            print(f"Error saving image: {e}")
            return None

async def download_player_image(submission_type: str, 
                                file_name: str,
                                player: Player,
                                attachment_url: str,
                                file_extension: str,
                                entry_id: int,  # Generic ID for any submission type
                                entry_name: str,  # Generic name for the entry
                                npc_name: str = ""):
    """
        Images should be stored in:
        /store/droptracker/disc/static/assets/img/user-upload/{player.wom_id}/{submission_type}/{npc_name (optional)}/{entry_name}_{entry_id}.{file_extension}
        This is served externally at:
        https://www.droptracker.io/img/user-upload/{player.wom_id}/{submission_type}/{npc_name (optional)}/{entry_name}_{entry_id}.{file_extension}
    """
    # Base internal directory path for storage
    base_dir = "/store/droptracker/disc/static/assets/img/user-upload/"
    
    # Base external URL for serving images
    base_url = "https://www.droptracker.io/img/user-upload/"

    # Ensure that npc_name is included only if provided
    if npc_name:
        directory_path = os.path.join(base_dir, str(player.wom_id), submission_type, npc_name)
        url_path = f"{player.wom_id}/{submission_type}/{npc_name}/"
    else:
        directory_path = os.path.join(base_dir, str(player.wom_id), submission_type)
        url_path = f"{player.wom_id}/{submission_type}/"

    # Ensure the directory structure exists
    os.makedirs(directory_path, exist_ok=True)

    # Generate unique filename for the download
    def generate_unique_filename(directory, file_name, ext):
        base_name = file_name
        counter = 1
        unique_file_name = f"{base_name}.{ext}"
        while os.path.exists(os.path.join(directory, unique_file_name)):
            unique_file_name = f"{base_name}_{counter}.{ext}"
            counter += 1
        return unique_file_name

    # Generate the full filename with entry_name and entry_id
    complete_file_name = f"{entry_name}_{entry_id}"
    unique_file_name = generate_unique_filename(directory_path, complete_file_name, file_extension)
    download_path = os.path.join(directory_path, unique_file_name)

    # Download the file asynchronously
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment_url) as response:
                if response.status == 200:
                    async with aiofiles.open(download_path, 'wb') as f:
                        while True:
                            chunk = await response.content.read(1024)
                            if not chunk:
                                break
                            await f.write(chunk)
        # Construct the external URL
        external_url = f"{base_url}{url_path}{unique_file_name}"
        return download_path, external_url
    except Exception as e:
        return None