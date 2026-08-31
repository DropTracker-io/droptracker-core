"""Per-player-per-NPC lifetime kill-count watermark (web108a).

The highest KC the plugin has ever reported for a (player, npc) pair, fed from
drop and personal-best submissions. This is the stored "previous" side of the
KC milestone crossing test (data/submissions/kc_milestones) — and, over time, a
lifetime-KC dataset drops.kill_count is too sparse to reconstruct.

kill_count is a watermark, not a counter: it only ever moves up, and only by
what a submission actually reported. Different counters for the same boss
(plugin chest count vs WOM metric — the Fortis Colosseum trap) make a large
backward or forward jump a re-seed signal rather than something to announce.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, func

from .base import Base


class PlayerNpcKc(Base):
    __tablename__ = "player_npc_kc"
    __table_args__ = (
        Index("ix_player_npc_kc_npc", "npc_id"),
        {"extend_existing": True},
    )

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    npc_id = Column(Integer, ForeignKey("npc_list.npc_id"), primary_key=True)
    kill_count = Column(Integer, nullable=False)
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
