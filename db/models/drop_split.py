from sqlalchemy import Column, Integer, ForeignKey, DateTime, event
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base, engine


class DropSplit(Base):
    """
    Records which players received split GP credit for a given drop within a group.

    One row per non-receiver split participant.  The receiver's credit is
    implicitly their full drop value minus the adjustment applied to the group
    leaderboard (tracked via Redis); this table is the source of truth used when
    force-rebuilding a player's Redis state.
    """
    __tablename__ = 'drop_splits'
    __table_args__ = {'extend_existing': True}

    id          = Column(Integer, primary_key=True, autoincrement=True)
    drop_id     = Column(Integer, ForeignKey('drops.drop_id'), nullable=False, index=True)
    player_id   = Column(Integer, ForeignKey('players.player_id'), nullable=False, index=True)
    group_id    = Column(Integer, ForeignKey('groups.group_id'), nullable=False, index=True)
    split_value = Column(Integer, nullable=False)
    date_added  = Column(DateTime, default=func.now())

    player = relationship("Player")
    drop   = relationship("Drop")


# Create the table on first import if it doesn't exist yet.
DropSplit.__table__.create(engine, checkfirst=True)
