from typing import List, Optional
from datetime import datetime, timedelta
import time
import pymysql
pymysql.install_as_MySQLdb()

from sqlalchemy import BigInteger, Text, Double, UniqueConstraint, ForeignKeyConstraint, create_engine, Table, Integer, Boolean, String, ForeignKey, DateTime, Float, text, Column, Index, Enum, TIMESTAMP
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


class Event(Base):
    """
    Represents an event in the database.
    :var id: The ID of the event
    :var name: The name of the event
    :var type: The type of event
    :var description: The description of the event
    :var start_date: The start date of the event
    :var status: The status of the event
    :var author_id: The ID of the author of the event
    """
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(DateTime, nullable=False)
    status = Column(String(255), nullable=False)
    author_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    update_number = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # The group relationship will be added in setup_relationships()
    
    # Other relationships
    participants = relationship("EventParticipant", back_populates="event")
    configurations = relationship("EventConfig", back_populates="event")
    teams = relationship("EventTeam", back_populates="event")
    items = relationship("EventItems", back_populates="event")


class EventConfig(Base):
    __tablename__ = 'event_configs'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    config_key = Column(String(255), nullable=False)
    config_value = Column(String(255), nullable=True)
    long_value = Column(LONGTEXT, nullable=True)
    update_number = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationships
    event = relationship("Event", back_populates="configurations")


class EventTeam(Base):
    __tablename__ = 'event_teams'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    current_location = Column(String(255), nullable=True)
    previous_location = Column(String(255), nullable=True)
    points = Column(Integer, nullable=False, default=0)
    gold = Column(Integer, nullable=False, default=100)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    current_task = Column(Integer, nullable=True)
    task_progress = Column(Integer, nullable=True)
    turn_number = Column(Integer, nullable=False, default=1)
    mercy_rule = Column(
        TIMESTAMP, 
        server_default=text("CURRENT_TIMESTAMP + INTERVAL 1 DAY"),
        nullable=True
    )
    mercy_count = Column(Integer, nullable=False, default=0)
    assembled_items = Column(String(255), nullable=True)

    # Relationships
    event = relationship("Event", back_populates="teams")
    members = relationship("EventParticipant", back_populates="team")
    inventory = relationship("EventTeamInventory", back_populates="team")
    cooldowns = relationship("EventTeamCooldown", back_populates="team")
    effects = relationship("EventTeamEffect", back_populates="team")


class EventItems(Base):
    __tablename__ = 'event_items'
    ## Items that can be used by a team in the event

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    cost = Column(Integer, nullable=False, default=0)
    effect = Column(Text, nullable=True)
    emoji = Column(String(255), nullable=True)
    item_type = Column(String(255), nullable=False)
    cooldown = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationships
    event = relationship("Event", back_populates="items")
    inventories = relationship("EventTeamInventory", back_populates="item")


class EventTeamInventory(Base):
    __tablename__ = 'event_team_inventory'

    id = Column(Integer, primary_key=True)
    event_team_id = Column(Integer, ForeignKey('event_teams.id'), nullable=False)
    item_id = Column(Integer, ForeignKey('event_items.id'), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationships
    team = relationship("EventTeam", back_populates="inventory")
    item = relationship("EventItems", back_populates="inventories")


class EventTeamCooldown(Base):
    __tablename__ = 'event_team_cooldowns'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'), nullable=False)
    cooldown_name = Column(String(255), nullable=False)
    remaining_turns = Column(Integer, nullable=False)
    expiry_date = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    
    # Relationship
    team = relationship("EventTeam", back_populates="cooldowns")


class EventTeamEffect(Base):
    __tablename__ = 'event_team_effects'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'), nullable=False)
    effect_name = Column(String(255), nullable=False)
    remaining_turns = Column(Integer, nullable=False)
    expiry_date = Column(DateTime, nullable=False)
    effect_data = Column(Text, nullable=True)  # For any additional effect data as JSON
    created_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    
    # Relationship
    team = relationship("EventTeam", back_populates="effects")


class EventParticipant(Base):
    __tablename__ = 'event_participants'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    player_id = Column(Integer, ForeignKey('players.player_id'), nullable=False)
    team_id = Column(Integer, ForeignKey('event_teams.id'), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    points = Column(String(255), nullable=False, default=0)

    # Relationships
    event = relationship("Event", back_populates="participants")
    # Use string references for User and Player
    user = relationship("User")
    player = relationship("Player")
    team = relationship("EventTeam", back_populates="members")

class EventTask(Base):
    __tablename__ = 'event_tasks'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    difficulty = Column(String(255), nullable=False)
    points = Column(Integer, nullable=False)
    required_items = Column(String(255), nullable=True)
    is_assembly = Column(Boolean, nullable=False)
    assembly_id = Column(Integer, nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())



# Setup database connection and create tables
engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data', pool_size=20, max_overflow=10)
Base.metadata.create_all(engine)
Session = scoped_session(sessionmaker(bind=engine))
session = Session()

