from interactions import ContextMenuContext, Message, message_context_menu, Permissions, Extension
import interactions

class ContextMenus(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        print("ContextMenus loaded")

