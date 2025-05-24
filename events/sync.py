"""
    Manages syncing event data between the internal database and XenForo
"""

import os
from dotenv import load_dotenv
import interactions
from interactions import ActionRow, Button, ButtonStyle, ContainerComponent, MediaGalleryComponent, MediaGalleryItem, Message, PartialEmoji, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, UnfurledMediaItem, listen
from xenforo.xenforo_api import XenforoAPI

class EventSync():
    def __init__(self):
        self.xf = XenforoAPI()
        pass

    def sync_events(self):
        pass

