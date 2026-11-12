"""Shadow comparison rows (web162a): WOM's number next to ours.

See ``utils/stats_shadow.py`` for how rows are written and what the columns
mean, and ``scripts/stats_shadow_report.py`` for reading them.
"""
from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Integer, String, UniqueConstraint

from .base import Base


class StatsShadowDiff(Base):
    __tablename__ = "stats_shadow_diffs"
    __table_args__ = (
        UniqueConstraint("consumer", "context", "player_id", "metric",
                         name="uq_stats_shadow_subject"),
        Index("ix_stats_shadow_consumer_seen", "consumer", "last_seen"),
        {"extend_existing": True},
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    consumer = Column(String(24), nullable=False)
    context = Column(String(64), nullable=False)
    # 0 when the comparison is not about a player (a rate table).
    player_id = Column(Integer, nullable=False, default=0)
    metric = Column(String(64), nullable=False)
    wom_value = Column(Float, nullable=True)
    our_value = Column(Float, nullable=True)      # NULL = we had no data
    our_source = Column(String(24), nullable=True)
    our_age_seconds = Column(Integer, nullable=True)
    detail = Column(String(255), nullable=True)
    observations = Column(Integer, nullable=False, default=1)
    worst_delta = Column(Float, nullable=True)    # largest |ours - wom| ever seen
    first_seen = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False)
