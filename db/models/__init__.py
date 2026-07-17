from .base import Base, session, xenforo_engine, XenforoSession, Session
from .associations import user_group_association
from .user import User
from .npc import NpcList
from .item import ItemList
from .item_value_override import ItemValueOverride
from .player import Player, IgnoredPlayer
from .group import Group
from .user_configuration import UserConfiguration
from .group_patreon import GroupPatreon
from .drop import Drop
from .drop_moderation import DropGroupModeration, EXCLUDING_STATUSES
from .collection import CollectionLogEntry
from .combat_achievement import CombatAchievementEntry
from .personal_best import PersonalBestEntry
from .player_pet import PlayerPet
from .quest_completion import QuestCompletionEntry
from .player_death import PlayerDeath
from .diary_completion import DiaryCompletionEntry
from .group_configuration import GroupConfiguration
from .group_notification import GroupNotification
from .notified_submission import NotifiedSubmission
from .notification_queue import NotificationQueue
from .embed import GroupEmbed, Field
from .guild_meta import Guild, GroupWomAssociation, GroupPersonalBestMessage, LBUpdate
from .webhooks import Webhook, BackupWebhook, WebhookPendingDeletion, NewWebhook
from .lootboard import LootboardStyle
from .analytics import (
    PlayerItemHourlyTotals,
    PlayerNpcHourlyTotals,
    GroupRecentDrops,
    PlayerDailyAggregates,
    PlayerLootData,
    PlayerExperience,
    HistoricalMetrics,
    Log,
)
from datetime import datetime
from .premium_features import ( 
    PremiumFeature, 
    FeatureActivation,
    PointCredit, 
    PointDebit, 
    RecurringPointGrant)
from .tickets import Ticket, TicketMessage
from .group_points import PlayerPoints, GroupPointConfig, GroupPointMods, GroupPointTimedEvent, GroupPointBlacklist, GroupPointSeason
from .video_upload import VideoUpload
from .seasonal_drop import SeasonalDrop
from .seasonal_personal_best import SeasonalPersonalBestEntry
from .seasonal_collection_log import SeasonalCollectionLogEntry
from .seasonal_combat_achievement import SeasonalCombatAchievementEntry
from .seasonal_pet import SeasonalPlayerPet
from .seasonal_quest_completion import SeasonalQuestCompletionEntry
from .drop_split import DropSplit
from .web import GroupAdmin, Announcement, AuditLog, DiscordOutbox, DocsPage, SiteRedirect, Suggestion, SuggestionMessage
from .badge import Badge, PlayerBadge
from .subscriptions import (
    SubscriptionTier,
    GroupSubscription,
    SubscriptionPayment,
    UserSubscription,
)
from .events import (
    Event,
    EventTask,
    EventTeam,
    EventTeamMember,
    EventSignup,
    EventBingoCell,
    EventBingoCompletion,
    EventCompletion,
    EventBuyin,
    EventProgress,
    EventChannel,
    EventLeaderVote,
    EventPlayerPoints,
    EventGroup,
    EventGuild,
    EventTaskLibraryItem,
    EventTemplate,
    EventMessageLayout,
    EventType,
    EventTypeTestGroup,
    EventBoardTile,
    EventBoardConfig,
    EventBoardPosition,
    EventCoinLedger,
    BoardgameShopItem,
    EventShopRotation,
    EventTeamInventory,
    EventTeamCooldown,
    EventBoardEffect,
    BOARDGAME_ITEM_TYPES,
    BOARDGAME_EFFECTS,
    BOARDGAME_INVENTORY_STATUSES,
    BOARDGAME_EFFECT_STATUSES,
    EVENT_KINDS,
    EVENT_TASK_DIFFICULTIES,
    EVENT_BOARD_TILE_KINDS,
    EVENT_BOARD_POSITION_STATUSES,
    EVENT_COIN_REASONS,
    EVENT_TASK_TYPES,
    EVENT_TASK_VISIBILITIES,
    EVENT_VISIBILITIES,
    EVENT_TEMPLATE_SCHEMA_VERSION,
    EVENT_FORMATION_MODES,
    EVENT_SELF_SIGNUP_MODES,
    EVENT_MODES,
    EVENT_GROUP_ROLES,
    EVENT_GROUP_STATUSES,
    EVENT_COMPLETION_STATUSES,
    EVENT_BUYIN_KINDS,
    EVENT_BUYIN_STATUSES,
    EVENT_PRIZE_DISTRIBUTIONS,
    EVENT_CHANNEL_KINDS,
    EVENT_BOARD_SIZES,
    EVENT_SUBMISSION_POLICIES,
    EVENT_DISCORD_POLICIES,
    EVENT_PING_KEYS,
    EVENT_TEAM_ROLES,
    EVENT_LEADER_SELECTION_MODES,
    EVENT_TASK_PROGRESS_MODES,
    EVENT_MESSAGE_TOGGLE_KEYS,
    EVENT_MESSAGE_LAYOUT_TYPES,
)


def get_current_partition() -> int:
    """
        Returns the naming scheme for a partition of drops
        Based on the current month
    """
    now = datetime.now()
    return now.year * 100 + now.month

__all__ = [
    "Base",
    "session",
    "Session",
    "xenforo_engine",
    "XenforoSession",
    "user_group_association",
    "User",
    "NpcList",
    "ItemList",
    "ItemValueOverride",
    "Player",
    "IgnoredPlayer",
    "Group",
    "UserConfiguration",
    "GroupPatreon",
    "Drop",
    "CollectionLogEntry",
    "CombatAchievementEntry",
    "PersonalBestEntry",
    "PlayerPet",
    "QuestCompletionEntry",
    "PlayerDeath",
    "DiaryCompletionEntry",
    "GroupConfiguration",
    "GroupNotification",
    "NotifiedSubmission",
    "NotificationQueue",
    "GroupEmbed",
    "Field",
    "Guild",
    "GroupWomAssociation",
    "GroupPersonalBestMessage",
    "LBUpdate",
    "Webhook",
    "BackupWebhook",
    "WebhookPendingDeletion",
    "NewWebhook",
    "PlayerItemHourlyTotals",
    "PlayerNpcHourlyTotals",
    "GroupRecentDrops",
    "PlayerDailyAggregates",
    "PlayerLootData",
    "PlayerExperience",
    "HistoricalMetrics",
    "Log",
    "PremiumFeature",
    "FeatureActivation",
    "PointCredit",
    "PointDebit",
    "RecurringPointGrant",
    "get_current_partition",
    "LootboardStyle",
    "Ticket",
    "TicketMessage",
    "PlayerPoints",
    "GroupPointConfig",
    "GroupPointMods",
    "GroupPointTimedEvent",
    "GroupPointBlacklist",
    "GroupPointSeason",
    "VideoUpload",
    "SeasonalDrop",
    "SeasonalPersonalBestEntry",
    "SeasonalCollectionLogEntry",
    "SeasonalCombatAchievementEntry",
    "SeasonalPlayerPet",
    "SeasonalQuestCompletionEntry",
    "DropSplit",
    "GroupAdmin",
    "Announcement",
    "AuditLog",
    "DiscordOutbox",
    "DocsPage",
    "SiteRedirect",
    "Suggestion",
    "SuggestionMessage",
    "Badge",
    "PlayerBadge",
    "SubscriptionTier",
    "GroupSubscription",
    "SubscriptionPayment",
    "UserSubscription",
    "Event",
    "EventTask",
    "EventTeam",
    "EventTeamMember",
    "EventSignup",
    "EventBingoCell",
    "EventBingoCompletion",
    "EventCompletion",
    "EventBuyin",
    "EventProgress",
    "EventChannel",
    "EventLeaderVote",
    "EventPlayerPoints",
    "EventGroup",
    "EventGuild",
    "EventTaskLibraryItem",
    "EventTemplate",
    "EventMessageLayout",
    "EventType",
    "EventTypeTestGroup",
    "EventBoardTile",
    "EventBoardConfig",
    "EventBoardPosition",
    "EventCoinLedger",
    "BoardgameShopItem",
    "EventShopRotation",
    "EventTeamInventory",
    "EventTeamCooldown",
    "EventBoardEffect",
    "BOARDGAME_ITEM_TYPES",
    "BOARDGAME_EFFECTS",
    "BOARDGAME_INVENTORY_STATUSES",
    "BOARDGAME_EFFECT_STATUSES",
    "EVENT_KINDS",
    "EVENT_TASK_DIFFICULTIES",
    "EVENT_BOARD_TILE_KINDS",
    "EVENT_BOARD_POSITION_STATUSES",
    "EVENT_COIN_REASONS",
    "EVENT_TEMPLATE_SCHEMA_VERSION",
    "EVENT_TASK_TYPES",
    "EVENT_TASK_VISIBILITIES",
    "EVENT_VISIBILITIES",
    "EVENT_FORMATION_MODES",
    "EVENT_SELF_SIGNUP_MODES",
    "EVENT_MODES",
    "EVENT_GROUP_ROLES",
    "EVENT_GROUP_STATUSES",
    "EVENT_COMPLETION_STATUSES",
    "EVENT_BUYIN_KINDS",
    "EVENT_BUYIN_STATUSES",
    "EVENT_PRIZE_DISTRIBUTIONS",
    "EVENT_CHANNEL_KINDS",
    "EVENT_DISCORD_POLICIES",
    "EVENT_PING_KEYS",
    "EVENT_TEAM_ROLES",
    "EVENT_LEADER_SELECTION_MODES",
    "DropGroupModeration",
    "EXCLUDING_STATUSES",
    "EVENT_BOARD_SIZES",
    "EVENT_SUBMISSION_POLICIES",
    "EVENT_TASK_PROGRESS_MODES",
    "EVENT_MESSAGE_TOGGLE_KEYS",
    "EVENT_MESSAGE_LAYOUT_TYPES",
]
