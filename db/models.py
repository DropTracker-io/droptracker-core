from typing import List, Optional
from datetime import datetime
import time
import pymysql
pymysql.install_as_MySQLdb()

from sqlalchemy import BigInteger, Text, Double, UniqueConstraint, ForeignKeyConstraint, create_engine, Table, Integer, Boolean, String, ForeignKey, DateTime, Float, text, Column, Index, Enum, TIMESTAMP, pool
from sqlalchemy.orm import relationship, scoped_session, sessionmaker, Mapped, declarative_base, relationship
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, LONGTEXT, TINYINT
from sqlalchemy import func
from dotenv import load_dotenv
import os
load_dotenv()

from db.base import Base

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")


def get_current_partition() -> int:
    """
        Returns the naming scheme for a partition of drops
        Based on the current month
    """
    now = datetime.now()
    return now.year * 100 + now.month


""" Define associations between users and players """
user_group_association = Table(
    'user_group_association', Base.metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('player_id', Integer, ForeignKey('players.player_id'), nullable=True),
    Column('user_id', Integer, ForeignKey('users.user_id'), nullable=True),
    Column('group_id', Integer, ForeignKey('groups.group_id'), nullable=False),
    UniqueConstraint('player_id', 'user_id', 'group_id', name='uq_user_group_player')
)

class NpcList(Base):
    """
        Stores the list of valid NPCs that are 
        being tracked individually for ranking purposes
        :param: npc_id: ID of the NPC based on OSRS Reboxed
        :param: npc_name: Name of the NPC based on OSRS Reboxed
    """
    __tablename__ = 'npc_list'
    npc_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    npc_name = Column(String(60), nullable=False)

class ItemList(Base):
    __tablename__ = 'items'
    item_id = Column(Integer, primary_key=True,nullable=False, index=True)
    item_name = Column(String(125), index=True)
    stackable = Column(Boolean, nullable=False, default=False)
    stacked = Column(Integer, nullable=False, default=0)
    noted = Column(Boolean, nullable=False)

class NotifiedSubmission(Base):
    """
    Drops that have exceeded the necessary threshold to have a notification
    sent to a Discord channel are stored in this table to allow modifications
    to be made to the message, drop, etc.
    """
    __tablename__ = 'notified'
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    channel_id = Column(String(35), nullable=False)
    message_id = Column(String(35))
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    status = Column(String(15))  # 'sent', 'removed', or 'pending'
    date_added = Column(DateTime, index=True, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    edited_by = Column(Integer, ForeignKey('users.user_id'), nullable=True)
    
    # Nullable foreign keys to allow only one relationship to be defined
    drop_id = Column(Integer, ForeignKey('drops.drop_id'), nullable=True)
    clog_id = Column(Integer, ForeignKey('collection.log_id'), nullable=True)
    ca_id = Column(Integer, ForeignKey('combat_achievement.id'), nullable=True)
    pb_id = Column(Integer, ForeignKey('personal_best.id'), nullable=True)

    # Relationships
    drop = relationship("Drop", back_populates="notified_drops")
    clog = relationship("CollectionLogEntry", back_populates="notified_clog")
    ca = relationship("CombatAchievementEntry", back_populates="notified_ca")
    pb = relationship("PersonalBestEntry", back_populates="notified_pb")

    def __init__(self, channel_id: str, 
                 message_id: str, 
                 group_id: int,
                 status: str, 
                 drop=None, 
                 clog=None, 
                 ca=None, 
                 pb=None):
        """
        Ensure that only one of drop, clog, ca, or pb can be defined.
        """
        if sum([bool(drop), bool(clog), bool(ca), bool(pb)]) > 1:
            raise ValueError("Only a single association can be provided to a NotifiedSubmission.")
        self.channel_id = channel_id
        self.message_id = message_id
        self.group_id = group_id
        self.status = status
        self.drop = drop
        self.clog = clog
        self.ca = ca
        self.pb = pb


## Notification Models

class GroupNotification(Base):
    __tablename__ = 'group_notifications'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(String(1000), nullable=False)
    message = Column(String(255), nullable=False)
    jump_url = Column(String(255), nullable=True)
    type = Column(String(255), nullable=False)
    player_id = Column(Integer, ForeignKey('players.player_id'), nullable=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    status = Column(String(255), nullable=False)


### Standard Models for Submissions, Groups, Players, Users, etc.

class Drop(Base):
    """
        :param: item_id
        :param: player_id
        :param: npc_id
        :param: value
        :param: quantity
        :param: image_url (nullable)
    """
    __tablename__ = 'drops'
    drop_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), index=True)
    player_id = Column(Integer, ForeignKey('players.player_id'), index=True, nullable=False)
    date_added = Column(DateTime, index=True, default=func.now())
    npc_id = Column(Integer, ForeignKey('npc_list.npc_id'), index=True)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    value = Column(Integer)
    quantity = Column(Integer)
    image_url = Column(String(150), nullable=True)
    authed = Column(Boolean, default=False)
    partition = Column(Integer, default=get_current_partition, index=True)
    
    player = relationship("Player", back_populates="drops")
    notified_drops = relationship("NotifiedSubmission", back_populates="drop")


class CollectionLogEntry(Base):
    """ 
        :param: item_id: The item ID for the item the user received
        :param: source: The NPC or source name that the drop was received from
        :param: player_id: The ID of the player who received the drop
        :param: reported_slots: The total log slots the user had when the submission arrived
    """
    __tablename__ = 'collection'
    log_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, index=True, nullable=False)
    npc_id = Column(Integer, ForeignKey('npc_list.npc_id'), nullable=False)
    player_id = Column(Integer, ForeignKey('players.player_id'), index=True, nullable=False)
    reported_slots = Column(Integer)
    image_url = Column(String(255), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

    player = relationship("Player", back_populates="clogs")
    notified_clog = relationship("NotifiedSubmission", back_populates="clog")


class CombatAchievementEntry(Base):
    """
        :param: player_id: Player ID who received this achievement
        :param: task_name: The name of the task they completed

    """
    __tablename__ = 'combat_achievement'
    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'))
    task_name = Column(String(255), nullable=False)
    image_url = Column(String(255), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())

    player = relationship("Player", back_populates="cas")
    notified_ca = relationship("NotifiedSubmission", back_populates="ca")


class PersonalBestEntry(Base):
    """
        Stores kill-time data for users
    """
    __tablename__ = 'personal_best'
    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'))
    npc_id = Column(Integer, ForeignKey('npc_list.npc_id'))
    kill_time = Column(Integer, nullable=False)
    personal_best = Column(Integer, nullable=False)
    team_size = Column(String(15), nullable=False, default="Solo")
    new_pb = Column(Boolean, default=False)
    image_url = Column(String(150), nullable=True)

    player = relationship("Player", back_populates="pbs")
    notified_pb = relationship("NotifiedSubmission", back_populates="pb")

class PlayerPet(Base):
    __tablename__ = 'player_pets'
    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'))
    item_id = Column(Integer, ForeignKey('items.item_id'))
    pet_name = Column(String(255), nullable=False)

    player = relationship("Player", back_populates="pets")

class Player(Base):
    """ 
    :param: wom_id: The player's WiseOldMan ID
    :param: player_name: The DISPLAY NAME of the player, exactly as it appears
    :param: user_id: The ID of the associated User object, if one exists
    :param: log_slots: Stored number of collected log slots
    :param: total_level: Account total level based on the last update with WOM.
        Defines the player object, which is instantly created any time a unique username
        submits a new drop/etc, and their WiseOldMan user ID doesn't already exist in our database.
    """
    __tablename__ = 'players'
    player_id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    wom_id = Column(Integer, unique=True)
    account_hash = Column(String(100), nullable=True, unique=True)
    player_name = Column(String(20), index=True)
    user_id = Column(Integer, ForeignKey('users.user_id'))
    log_slots = Column(Integer)
    total_level = Column(Integer)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    hidden = Column(Boolean, default=False)

    pbs = relationship("PersonalBestEntry", back_populates="player")
    cas = relationship("CombatAchievementEntry", back_populates="player")
    clogs = relationship("CollectionLogEntry", back_populates="player")  # Add this line
    pets = relationship("PlayerPet", back_populates="player")
    
    user = relationship("User", back_populates="players")
    drops = relationship("Drop", back_populates="player")
    groups = relationship("Group", secondary=user_group_association, back_populates="players")

    def add_group(self, group):
        # Check if the association already exists by querying the user_group_association table
        existing_association = session.query(user_group_association).filter_by(
            player_id=self.player_id, group_id=group.group_id).first()
        if self.user:
            tuser: User = self.user
            tuser.add_group(group)
        if not existing_association:
            # Only add the group if no association exists
            self.groups.append(group)
            session.commit()
            
    def remove_group(self, group):
        # Check if the association already exists by querying the user_group_association table
        existing_association = session.query(user_group_association).filter_by(
            player_id=self.player_id, group_id=group.group_id).first()
        if existing_association:
            self.groups.remove(group)
            session.commit()

    def get_current_total(self):
        from utils.redis import RedisClient
        redis_client = RedisClient()
        try:
            partition = datetime.now().year * 100 + datetime.now().month
            total_loot_key = f"player:{self.player_id}:{partition}:total_loot"
            # Get total items
            total_loot = redis_client.client.get(total_loot_key)
            #print("redis update total items stored:", total_items)
            if total_loot:
                total_loot = int(total_loot.decode('utf-8'))
            else:
                total_loot = 0
            return total_loot
        except Exception as e:
            print(f"Error getting current total for player {self.player_id}: {e}")
            return 0

    def __init__(self, wom_id, player_name, account_hash, user_id=None, user=None, log_slots=0, total_level=0, group=None, hidden=False):
        self.wom_id = wom_id
        self.player_name = player_name
        self.account_hash = account_hash
        self.user_id = user_id
        self.user = user
        self.log_slots = log_slots
        self.total_level = total_level
        self.hidden = hidden
        self.group = group

class User(Base):
    """
        :param: discord_id: The string formatted representation of the user's Discord ID
        :param: username: The user's Discord display name
        :param: patreon: The patreon subscription status of the user
    """
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True, autoincrement=True)
    discord_id = Column(String(35))
    date_added = Column(DateTime, default=func.now())
    auth_token = Column(String(16), nullable=False)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    username = Column(String(20))
    xf_user_id = Column(Integer, nullable=True)
    public = Column(TINYINT(1), server_default=text('1'))
    global_ping = Column(Boolean, default=False)
    group_ping = Column(Boolean, default=False)
    never_ping = Column(Boolean, default=False)
    hidden = Column(Boolean, default=False)
    players = relationship("Player", back_populates="user")
    groups = relationship("Group", secondary=user_group_association, back_populates="users", overlaps="groups")
    configurations = relationship("UserConfiguration", back_populates="user")
    group_patreon = relationship("GroupPatreon", back_populates="user")

    def add_group(self, group):
        # Check if the association already exists by querying the user_group_association table
        existing_association = session.query(user_group_association).filter_by(
            user_id=self.user_id, group_id=group.group_id).first()

        if not existing_association:
            # Only add the group if no association exists
            self.groups.append(group)
            session.commit()


class Group(Base):
    """
    :param: group_name: Publicly-displayed name of the group
    :param: wom_id: WiseOldMan group ID associated with the Group
    :param: guild_id: Discord Guild ID, if one is associated with it
    """
    __tablename__ = 'groups'
    group_id = Column(Integer, primary_key=True, autoincrement=True)
    group_name = Column(String(30), index=True)
    description = Column(String(255), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    wom_id = Column(Integer, default=None)
    guild_id = Column(String(255), default=None, nullable=True)
    invite_url = Column(String(255), default=None, nullable=True)
    icon_url = Column(String(255), default=None, nullable=True)

    # Relationships
    configurations = relationship("GroupConfiguration", back_populates="group")
    # drops = relationship("Drop", back_populates="group")
    players = relationship("Player", secondary=user_group_association, back_populates="groups", overlaps="groups", lazy='dynamic')
    users = relationship("User", secondary=user_group_association, back_populates="groups", overlaps="groups,players")
    group_patreon = relationship("GroupPatreon", back_populates="group")
    group_embeds = relationship("GroupEmbed", back_populates="group")
    # One-to-One relationship with Guild
    guild = relationship("Guild", back_populates="group", uselist=False, cascade="all, delete-orphan")

    def add_player(self, player):
        # Check if the association already exists
        existing_association = self.players.filter(user_group_association.c.player_id == player.player_id).first()
        if not existing_association:
            # Only add the player if no association exists
            self.players.append(player)
            session.commit()

    def get_current_total(self):
        try:
            total_value = 0
            from utils.redis import RedisClient
            redis_client = RedisClient()
            for player in self.players:
                """
                Get the true, most accurate player total from Redis
                """
                partition = datetime.now().year * 100 + datetime.now().month
                total_loot_key = f"player:{player.player_id}:{partition}:total_loot"
                # Get total items
                total_loot = redis_client.client.get(total_loot_key)
                #print("redis update total items stored:", total_items)
                if total_loot:
                    total_loot = int(total_loot.decode('utf-8'))
                    total_value += total_loot
                else:
                    total_value += 0
            return total_value
        except Exception as e:
            print(f"Error getting current total for group {self.group_id}: {e}")
            return 0

    def __init__(self, group_name, wom_id, guild_id, description: str= "An Old School RuneScape group."):
        self.group_name = group_name
        self.wom_id = wom_id
        self.guild_id = guild_id
        self.description = description
        
    def after_insert(self):
        """Calls after a group is created"""
        pass
        #create_xf_group(self, group_id=self.group_id)

class PlayerMetric(Base):
    __tablename__ = 'player_snapshots'
    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'), nullable=False)
    metric_name = Column(String(255), nullable=False)
    metric_value = Column(Integer, nullable=False)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

class GroupPersonalBestMessage(Base):
    __tablename__ = 'group_personal_best_message'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    message_id = Column(String(255), nullable=False)
    channel_id = Column(String(255), nullable=False)
    boss_name = Column(String(255), nullable=False)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

class GroupPatreon(Base):
    __tablename__ = 'group_patreon'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=True)
    patreon_tier = Column(Integer, nullable=False)  ## A user needs to be tier 2 
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

    user = relationship("User", back_populates="group_patreon")
    group = relationship("Group", back_populates="group_patreon")


class LootboardStyle(Base):
    __tablename__ = 'lootboards'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(String(255), nullable=False)
    local_url = Column(String(255), nullable=False)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())


class PatreonNotification(Base):
    __tablename__ = 'patreon_notification'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    discord_id = Column(String(255), nullable=True)
    user_id = Column(Integer, nullable=True)
    timestamp = Column(TIMESTAMP, nullable=False, 
                       server_default=text('current_timestamp() ON UPDATE current_timestamp()'))
    tier = Column(String(255), nullable=True)
    status = Column(Integer, nullable=True)
    
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'mysql_default_charset': 'utf8mb4',
        'mysql_collate': 'utf8mb4_general_ci'
    }
    
class Ticket(Base):
    __tablename__ = 'tickets'
    ticket_id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    created_by = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    claimed_by = Column(Integer, ForeignKey('users.user_id'), nullable=True)
    status = Column(String(255), nullable=False)
    date_added = Column(DateTime, default=func.now())
    last_reply_uid = Column(String(255), nullable=True)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

class GroupConfiguration(Base):
    __tablename__ = 'group_configurations'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    config_key = Column(String(60), nullable=False)
    config_value = Column(String(255), nullable=False)
    long_value = Column(LONGTEXT, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    group = relationship("Group", back_populates="configurations")


class UserConfiguration(Base):

    __tablename__ = 'user_configurations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    config_key = Column(String(60), nullable=False)
    config_value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="configurations")

class Guild(Base):
    """
    :param: guild_id: Discord guild_id, string-formatted.
    :param: group_id: Respective group_id, if one already exists
    :param: date_added: Time the guild was generated
    :param: initialized: Status of the guild's registration (do they have a Group associated?)
    """
    __tablename__ = 'guilds'
    guild_id = Column(String(255), primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    initialized = Column(Boolean, default=False)

    # One-to-One relationship with Group
    group = relationship("Group", back_populates="guild", single_parent=True, uselist=False)


class GroupEmbed(Base):
    """
        Represents an embed that is sent for drops, collection logs, loot leaderboards, etc.
        :param: color: Hexidecimal representation of the color
        :param: title: String title of the embed
        :param: description: String description of the embed
        :param: thumbnail String url: 'https://i.imgur.com/AfFp7pu.png'
        :param: timestamp: Boolean - true = displayed
        :param: image: String url: 'https://i.imgur.com/AfFp7pu.png'
    """
    __tablename__ = 'group_embeds'
    embed_id = Column(Integer, primary_key=True, autoincrement=True)
    embed_type = Column(String(10)) # "lb", "drop", "clog", "ca", "pb"
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False, default=1)
    color = Column(String(7),nullable=True)
    title = Column(String(255),nullable=False)
    description = Column(String(1000),nullable=True)
    thumbnail = Column(String(200))
    timestamp = Column(Boolean,nullable=True,default=False)
    image = Column(String(200),nullable=True)

    fields = relationship("Field", back_populates="embed", cascade="all, delete-orphan")
    group = relationship("Group", back_populates="group_embeds")

class Field(Base):
    __tablename__ = "group_embed_fields"
    field_id = Column(Integer, primary_key=True, autoincrement=True)
    embed_id = Column(Integer, ForeignKey('group_embeds.embed_id'), nullable=False)
    field_name = Column(String(256), nullable=False)
    field_value = Column(String(1024), nullable=False)
    inline = Column(Boolean, default=True)

    embed = relationship("GroupEmbed", back_populates="fields")

class Webhook(Base):
    __tablename__ = 'webhooks'
    webhook_id = Column(Integer, primary_key=True)
    webhook_url = Column(String(255), unique=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

class NewWebhook(Base):
    __tablename__ = 'new_webhooks'
    webhook_id = Column(Integer, primary_key=True)
    webhook_hash = Column(Text, unique=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())



class Log(Base):
    """Model for storing application logs"""
    __tablename__ = 'logs'
    
    id = Column(Integer, primary_key=True)
    level = Column(String(10), nullable=False)  # INFO, WARNING, ERROR, DEBUG
    source = Column(String(50), nullable=False)  # Which part of the application
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # For storing stack traces or additional data
    timestamp = Column(BigInteger, index=True, default=lambda: int(time.time()))
    
    def __repr__(self):
        return f"<Log(id={self.id}, level={self.level}, source={self.source}, timestamp={self.timestamp})>" 










#### Below models are not used currently; but need to be kept to avoid errors.





class CronLog(Base):
    __tablename__ = 'cron_log'
    __table_args__ = (
        Index('idx_executed_at', 'executed_at'),
        Index('idx_job_name', 'job_name')
    )

    id = Column(BIGINT(20), primary_key=True)
    job_name = Column(String(255), nullable=False)
    status = Column(Enum('success', 'error'), nullable=False)
    executed_at = Column(TIMESTAMP, nullable=False)
    message = Column(Text)




class ForumSections(Base):
    __tablename__ = 'forum_sections'
    __table_args__ = (
        ForeignKeyConstraint(['category_id'], ['forum_categories.category_id'], name='forum_sections_ibfk_1'),
        Index('category_id', 'category_id')
    )

    section_id = Column(INTEGER(11), primary_key=True)
    name = Column(String(100), nullable=False)
    category_id = Column(INTEGER(11))
    description = Column(Text)
    icon = Column(String(50))
    display_order = Column(INTEGER(11), server_default=text('0'))
    created_at = Column(TIMESTAMP, server_default=text('current_timestamp()'))



class ForumThreads(Base):
    __tablename__ = 'forum_threads'
    __table_args__ = (
        ForeignKeyConstraint(['section_id'], ['forum_sections.section_id'], name='forum_threads_ibfk_1'),
        ForeignKeyConstraint(['user_id'], ['users.user_id'], name='forum_threads_ibfk_2'),
        Index('section_id', 'section_id'),
        Index('user_id', 'user_id')
    )

    thread_id = Column(INTEGER(11), primary_key=True)
    title = Column(String(200), nullable=False)
    section_id = Column(INTEGER(11))
    user_id = Column(INTEGER(11))
    content = Column(Text)
    is_pinned = Column(TINYINT(1), server_default=text('0'))
    is_locked = Column(TINYINT(1), server_default=text('0'))
    view_count = Column(INTEGER(11), server_default=text('0'))
    created_at = Column(TIMESTAMP, server_default=text('current_timestamp()'))
    updated_at = Column(TIMESTAMP, server_default=text('current_timestamp() ON UPDATE current_timestamp()'))


class ForumPosts(Base):
    __tablename__ = 'forum_posts'
    __table_args__ = (
        ForeignKeyConstraint(['thread_id'], ['forum_threads.thread_id'], name='forum_posts_ibfk_1'),
        ForeignKeyConstraint(['user_id'], ['users.user_id'], name='forum_posts_ibfk_2'),
        Index('thread_id', 'thread_id'),
        Index('user_id', 'user_id')
    )

    post_id = Column(INTEGER(11), primary_key=True)
    thread_id = Column(INTEGER(11))
    user_id = Column(INTEGER(11))
    content = Column(Text)
    created_at = Column(TIMESTAMP, server_default=text('current_timestamp()'))
    updated_at = Column(TIMESTAMP, server_default=text('current_timestamp() ON UPDATE current_timestamp()'))



class ForumReactions(Base):
    __tablename__ = 'forum_reactions'
    __table_args__ = (
        ForeignKeyConstraint(['post_id'], ['forum_posts.post_id'], name='forum_reactions_ibfk_1'),
        ForeignKeyConstraint(['user_id'], ['users.user_id'], name='forum_reactions_ibfk_2'),
        Index('post_id', 'post_id'),
        Index('user_id', 'user_id')
    )

    reaction_id = Column(INTEGER(11), primary_key=True)
    post_id = Column(INTEGER(11))
    user_id = Column(INTEGER(11))
    reaction_type = Column(String(20))
    created_at = Column(TIMESTAMP, server_default=text('current_timestamp()'))


class ForumCategories(Base):
    __tablename__ = 'forum_categories'

    category_id = Column(INTEGER(11), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    icon = Column(String(50))
    display_order = Column(INTEGER(11), server_default=text('0'))
    created_at = Column(TIMESTAMP, server_default=text('current_timestamp()'))




class XfDtCronLogs(Base):
    __tablename__ = 'xf_dt_cron_logs'
    __table_args__ = (
        Index('cron_id', 'cron_id'),
        Index('start_date', 'start_date'),
        Index('status', 'status')
    )

    log_id = Column(INTEGER(10), primary_key=True)
    cron_id = Column(String(50), nullable=False)
    execution_time = Column(Double(asdecimal=True), nullable=False, server_default=text('0'))
    start_date = Column(INTEGER(10), nullable=False)
    end_date = Column(INTEGER(10), nullable=False)
    status = Column(Enum('success', 'error', 'warning', 'info'), nullable=False, server_default=text("'info'"))
    context = Column(String(100), nullable=False, server_default=text("''"))
    memory_usage = Column(INTEGER(10), nullable=False, server_default=text('0'))
    peak_memory_usage = Column(INTEGER(10), nullable=False, server_default=text('0'))
    message = Column(Text)
    error_message = Column(Text)

class HistoricalMetrics(Base):
    __tablename__ = 'historical_metrics'
    __table_args__ = (
        Index('idx_type_timestamp', 'metric_type', 'timestamp'),
    )

    id = Column(INTEGER(11), primary_key=True)
    metric_type = Column(String(50), nullable=False)
    value = Column(INTEGER(11), nullable=False)
    timestamp = Column(TIMESTAMP, server_default=text('current_timestamp()'))

# Setup database connection and create tables
engine = create_engine(
    f"mysql+pymysql://{DB_USER}:{DB_PASS}@localhost/data",
    pool_size=20,                  # Adjust based on your needs
    max_overflow=10,               # Allow creating extra connections when pool is full
    pool_timeout=30,               # Wait time for connection from pool
    pool_recycle=3600,             # Recycle connections after 1 hour
    pool_pre_ping=True,            # Verify connections before use
    isolation_level="READ COMMITTED"  # Prevent long-running transactions
)

# Create a session factory that creates new sessions as needed
Session = sessionmaker(bind=engine)

# Create a thread-local session registry
session = scoped_session(Session)

# xenforo_engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/xf', pool_size=20, max_overflow=10)
# xenforo_session = sessionmaker(bind=xenforo_engine)()

# def sync_xf_groups():
#     try:
#         groups = session.query(Group).all()
#         for group in groups:
#             print("Syncing group:", group.group_name)
#             try:
#                 # Fetch the group using raw SQL
#                 result = xenforo_session.execute(
#                     text("SELECT * FROM xf_droptracker_group WHERE external_id = :external_id"),
#                     {"external_id": group.group_id}
#                 ).fetchone()  # Use fetchone() to get a single result

#                 print("Result row:", result)
#                 if result:
#                     # Define the expected column names based on the database schema
#                     column_names = [
#                         'group_id', 'name', 'description', 'external_id', 'is_active',
#                         'last_sync', 'sync_error', 'is_public', 'creation_date', 
#                         'invite_url', 'current_total', 'icon_url', 'wom_id', 'guild_id'
#                     ]
#                     # Convert the result to a dictionary using the actual column names
#                     xf_group = {column: value for column, value in zip(column_names, result)}
#                 else:
#                     xf_group = None  # Handle the case where no group is found

#                 if not xf_group:
#                     create_xf_group(group)
#                     print("A new group has been created in XenForo:", group.group_name)
#                     create_xf_servicelog("info", f"A new group has been created in DropTracker: {group.group_name} ({group.group_id})")
#                 else:
#                     print("Group already exists in XenForo:", group.group_name)
                    
#                     # Attempt to update attributes using dictionary-like access
#                     if xf_group['wom_id'] != group.wom_id:
#                         print(f"Current wom_id: {xf_group['wom_id']}, New wom_id: {group.wom_id}")
#                         xf_group['wom_id'] = group.wom_id

#                     if xf_group['guild_id'] != group.guild_id:
#                         print(f"Current guild_id: {xf_group['guild_id']}, New guild_id: {group.guild_id}")
#                         xf_group['guild_id'] = group.guild_id

#                     # Commit changes using a raw SQL update
#                     xenforo_session.execute(
#                         text("""
#                             UPDATE xf_droptracker_group
#                             SET wom_id = :wom_id, guild_id = :guild_id
#                             WHERE external_id = :external_id
#                         """),
#                         {
#                             "wom_id": xf_group['wom_id'],
#                             "guild_id": xf_group['guild_id'],
#                             "external_id": group.group_id
#                         }
#                     )
#                     xenforo_session.commit()
#                     print("Updated group settings for a group.")
#                     create_xf_servicelog("info", f"Group wom ID or guild ID updated for {group.group_name} ({group.group_id})")
                
#                 print("Checking configs")
#                 configs = session.query(GroupConfiguration).filter_by(group_id=group.group_id).all()
#                 for config in configs:
#                     xf_config = xenforo_session.execute(
#                         text("SELECT * FROM xf_droptracker_group_configurations WHERE external_id = :external_id AND config_key = :config_key"),
#                         {"external_id": group.group_id, "config_key": config.config_key}
#                     ).fetchone()

#                     if xf_config:
#                         # Define the expected column names for the config
#                         config_column_names = [
#                             'config_id', 'external_id', 'config_key', 'config_value', 'long_value'
#                         ]
#                         xf_config_dict = {column: value for column, value in zip(config_column_names, xf_config)}  # Convert to dictionary
#                     else:
#                         xf_config_dict = None  # Handle case where no config is found

#                     if not xf_config_dict:
#                         xenforo_session.execute(text("""
#                             INSERT INTO xf_droptracker_group_configurations (external_id, config_key, config_value, long_value)
#                             VALUES (:external_id, :config_key, :config_value, :long_value)
#                         """), {
#                             "external_id": group.group_id,
#                             "config_key": config.config_key,
#                             "config_value": config.config_value,
#                             "long_value": config.long_value
#                         })
#                         xenforo_session.commit()
#                         print("Created new configuration settings for a group.")
#                         create_xf_servicelog("info", f"New configuration settings created for {group.group_name} ({group.group_id})")
#             except Exception as e:
#                 print(f"Error syncing group {group.group_name}: {e}")
#                 xenforo_session.rollback()  # Rollback in case of error
#     except Exception as e:
#         print(f"Error fetching groups: {e}")
#     finally:
#         # Ensure to close the session if needed
#         xenforo_session.close()

# def create_xf_group(Group: Group, group_id: int):
#     # Setup database connection for XenForo
#     params = {
#         "name": Group.group_name,
#         "description": "An Old School RuneScape group.",
#         "external_id": group_id,
#         "is_active": True,
#         "last_sync": int(datetime.now().timestamp()),
#         "sync_error": '',
#         "is_public": True,
#         "creation_date": int(datetime.now().timestamp()),
#         "invite_url": '',
#         "wom_id": Group.wom_id,
#         "guild_id": Group.guild_id,
#         "icon_url": 'https://www.droptracker.io/img/droptracker-small.gif'
#     }
#     xenforo_session.execute(text("""
#         INSERT INTO xf_droptracker_group (name, description, external_id, is_active, last_sync, sync_error, is_public, creation_date, invite_url, wom_id, guild_id, icon_url)
#         VALUES (:name, :description, :external_id, :is_active, :last_sync, :sync_error, :is_public, :creation_date, :invite_url, :wom_id, :guild_id, :icon_url)
#     """), params)
#     xenforo_session.commit()
#     create_xf_servicelog("info", f"A new group has been created in DropTracker: {Group.group_name} ({Group.group_id})")

# def create_xf_servicelog(log_level: str, message: str):
#     params = {
#         "log_level": log_level,
#         "message": message
#     }
#     xenforo_session.execute(text("""
#         INSERT INTO xf_droptracker_service_logs (log_level, message)
#         VALUES (:log_level, :message)
#     """), params)
#     xenforo_session.commit()

# def add_xf_npc(npc_name: str, npc_id: int):
#     xenforo_session.execute(text("""
#         INSERT INTO xf_droptracker_npc (npc_name, npc_id)
#         VALUES (:npc_name, :npc_id)
#     """), {"npc_name": npc_name, "npc_id": npc_id})
#     create_xf_servicelog("info", f"A new NPC has been added to the database: {npc_name} ({npc_id})")
#     xenforo_session.commit()

# def add_xf_item(item_name: str, item_id: int, noted: bool):
#     xenforo_session.execute(text("""
#         INSERT INTO xf_droptracker_item (item_name, item_id, noted)
#         VALUES (:item_name, :item_id, :noted)
#     """), {"item_name": item_name, "item_id": item_id, "noted": noted})
#     create_xf_servicelog("info", f"A new item has been added to the database: {item_name} ({item_id})")
#     xenforo_session.commit()