### Defines all shop items available to be used in the board game shop.

from dataclasses import dataclass
from enum import Enum
from games.events.utils.classes.base import ShopItem, ShopItemType

class DinhsBulwark(ShopItem):
    id = 1
    name = "Dinh's Bulwark"
    default_cost = 1000
    emoji = "<:bulwark:1348800005972033658>"
    effect = "Blocks enemy teams from passing through the tile the team places it on."
    effect_long = "A team using a Dinh's Bulwark will leave it behind on their current tile, blocking other teams from passing freely.\n" + \
             "The bulwark is only removed when it `procs` -- or when a team *would have* moved to a tile beyond it but got blocked instead."
    item_type = ShopItemType.DEFENSIVE
    cooldown = 3

