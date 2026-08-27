"""XenForo forum alerts.

Both calls below target **production** www.droptracker.io with a hardcoded API
key, regardless of which instance is running them — so a dev instance reaching
this module writes to the live forum. Nothing imports it today, but it is one
`from services.xf_services import ...` away from doing so, and mirrored
production traffic would drive it at production rates.

The key on line below is a live credential in tracked source and should be
rotated; that is deliberately not folded into this guard.
"""
import aiohttp
import asyncio

xf_key = "y0goxY3I9v5ZsD_PFEDOl5cwE2oGN58k"
user_id = 1
headers = {
    'XF-Api-User': f'{user_id}',
    'XF-Api-Key': f'{xf_key}'
}


def _is_dev() -> bool:
    """A dev instance must not write to the production forum."""
    from utils.dev_guild_guard import is_dev_mode

    return is_dev_mode()


async def get_user_id(player_id: int):
    if _is_dev():
        return None
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://www.droptracker.io/api/player/{player_id}/get-user-id', headers=headers) as response:
            data = await response.json()
            return data.get('user_id', None)
        
async def create_alert(user_id: int, alert: str, link_url: str, link_title: str):
    if _is_dev():
        return None
    data = {
        "to_user_id": user_id, ## The user ID of the user who will receive the alert
        "alert": alert, ## The text shown in the alert
        "from_user_id": user_id, ## The user ID of the user who created the alert
        "link_url": link_url, ## The URL of the link
        "link_title": link_title ## The title of the link
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f'https://www.droptracker.io/api/alerts', headers=headers, json=data) as response:
            return await response.json()