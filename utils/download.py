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
        url_path = f"{player.wom_id}/{sub_type}/{processed_data.get('npc_name', None)}/"

        def generate_unique_filename(directory, file_name, ext):
            base_name = file_name
            counter = 1
            unique_file_name = f"{base_name}.{ext}"
            while os.path.exists(os.path.join(directory, unique_file_name)):
                unique_file_name = f"{base_name}_{counter}.{ext}"
                counter += 1
            return unique_file_name
        
        try:
            # Generate unique filename
            if sub_type == "drop":
                directory_path = os.path.join(directory_path, processed_data.get("source", None))
                item_name = processed_data.get("item", None)
                npc_name = processed_data.get("source", None)
                if item_name and npc_name:
                    filename = f"{npc_name}_{item_name}.jpg"
                    if os.path.exists(os.path.join(directory_path, filename)):
                        filename = f"{npc_name}_{item_name}_{uuid.uuid4()}.jpg"
                else:
                    filename = f"{item_name}.jpg"
            elif sub_type == "pb":
                directory_path = os.path.join(directory_path, processed_data.get("boss_name", None))
                filename = f"{processed_data.get('npc_name', None)}_{processed_data.get('team_size', None)}_{processed_data.get('time', None)}.jpg"
                if os.path.exists(os.path.join(directory_path, filename)):
                    filename = generate_unique_filename(directory_path, filename, "jpg")
            elif sub_type == "clog":
                directory_path = os.path.join(directory_path, processed_data.get("source", None))
                filename = f"{processed_data.get('item', None)}.jpg"
                if os.path.exists(os.path.join(directory_path, filename)):
                    filename = generate_unique_filename(directory_path, filename, "jpg")
            elif sub_type == "ca":
                directory_path = os.path.join(directory_path, processed_data.get("task_name", None))
                filename = f"{processed_data.get('task_name', None)}_{processed_data.get('task_tier', None)}.jpg"
                if os.path.exists(os.path.join(directory_path, filename)):
                    filename = generate_unique_filename(directory_path, filename, "jpg")
            else:
                filename = f"{processed_data.get('entry_name', None)}_{processed_data.get('entry_id', None)}.jpg"
                if os.path.exists(os.path.join(directory_path, filename)):
                    filename = generate_unique_filename(directory_path, filename, "jpg")
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