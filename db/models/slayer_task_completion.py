from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class SlayerTaskCompletionEntry(Base):
    """One completed slayer task, as the plugin observed it.

    The source for ``slayer_target`` event goals ("25 tasks", "10 from
    Duradel"). Nothing else can supply the number: hiscores and Wise Old Man
    expose no task count, so a completion the plugin did not see is one we
    never learn about.

    ``master_id`` is the RAW ``SLAYER_MASTER`` varbit value. It is kept beside
    the resolved name on purpose: only two of the ten ids are confirmed at the
    time of writing (see ``utils/slayer_masters.py``), and the raw id is what
    lets a registry correction fix history rather than only the future. The
    same goes for ``completion_message`` — the chat line the numbers were
    parsed from, so a parse fix can be replayed.
    """

    __tablename__ = "slayer_task_completions"
    __table_args__ = (
        # "This player's tasks during the event window" — the shape every
        # event/profile read takes.
        Index("ix_slayer_task_completions_player_date", "player_id", "date_added"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    # Assignment name as the game cache spells it ("Cave kraken", "The
    # Alchemical Hydra"); utils.slayer_masters.SLAYER_TASK_NAMES is the catalog.
    task_name = Column(String(120), nullable=False)
    # SLAYER_TARGET varp (395); 98 means a boss task, whose boss is boss_id.
    task_id = Column(Integer, nullable=True)
    boss_id = Column(Integer, nullable=True)
    # SLAYER_MASTER varbit (4067), raw — see the class note.
    master_id = Column(SmallInteger, nullable=True, index=True)
    master_name = Column(String(40), nullable=True)
    # SLAYER_AREA varp (2096): Konar's location-locked assignments.
    area_id = Column(Integer, nullable=True)
    amount_initial = Column(Integer, nullable=True)
    amount_killed = Column(Integer, nullable=True)
    # Task streak AFTER this completion, as the game reported it.
    streak = Column(Integer, nullable=True)
    points_awarded = Column(Integer, nullable=True)
    points_total = Column(Integer, nullable=True)
    xp_gained = Column(Integer, nullable=True)
    completion_message = Column(String(255), nullable=True)
    world_type = Column(String(20), nullable=False, default="main")
    timestamp = Column(Integer, nullable=True)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True, unique=True, index=True)

    player = relationship("Player", back_populates="slayer_tasks")
