import os
import requests

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("XF_KEY")

class XenforoAPI:
    """
        Xenforo API wrapper for interacting with the Events system that is primarily hosted
        inside of XenForo.
    """
    def __init__(self):
        self.api_key = API_KEY
        self.base_url = "https://www.droptracker.io/api"
        self.headers = {
            "XF-Api-Key": f"{API_KEY}",
            "XF-Api-User": f"1"
        }

    def get_active_events(self):
        # Get active events
        response = requests.get(f'{self.base_url}/events/active', headers=self.headers)
        active_events = response.json()['events']

        return active_events

    def get_event(self, event_id):
        response = requests.get(f'{self.base_url}/events/{event_id}', headers=self.headers)
        event = response.json()

        return event

