import aiofiles
import os
import re
from db.models import Player

import aiohttp


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