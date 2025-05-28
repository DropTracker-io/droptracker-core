from enum import Enum as PyEnum
class TaskType(PyEnum):
    """Enumeration of different task types"""
    ITEM_COLLECTION = "item_collection"
    ANY_ITEM = "any_item"
    SET_COLLECTION = "set_collection"
    KC_TARGET = "kc_target"
    XP_TARGET = "xp_target"
    EHP_TARGET = "ehp_target"
    EHB_TARGET = "ehb_target"
    LOOT_VALUE = "loot_value"
    CUSTOM = "custom"