"""Server-controlled reference data the RuneLite plugin reads at startup.

The plugin needs to know *which* game values to read before it can read them:
the varps holding combat-achievement completion bits, the quest ids to poll for
state, and (later) the collection log's page/slot structure. Baking those into
the plugin means a Plugin Hub release — and a review queue — every time Jagex
adds content, during which we silently under-report.

The combat achievement varps are the clearest case. They are **not** a
contiguous range: as of RuneLite 1.12.35 they are 3116-3128, then 3387, 3718,
3773, 3774, 4204, 4496, 4721, 5673. Jagex appends a new varp at an arbitrary id
whenever the previous one runs out of bits, so a hardcoded range does not merely
go stale, it silently drops whole batches of tasks with no error anywhere.

Stored as independent sections rather than one blob so each can be regenerated
on its own cadence (quest ids change on a quest release, clog structure on a
content drop) without a writer having to round-trip the others. The assembled
manifest's ``version`` is derived from the content hash — see
``manifest_payload()`` — so there is no version integer for anyone to forget to
bump.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, String, Text, func

from .base import Base


class PluginManifestSection(Base):
    __tablename__ = "plugin_manifest_sections"
    __table_args__ = ({"extend_existing": True},)

    # Section name as it appears in the assembled manifest JSON, e.g.
    # "combat_achievement_varps". Also the natural key the regeneration script
    # upserts on.
    key = Column(String(64), primary_key=True)
    # JSON, shape depends on the section. Text rather than a JSON column: we
    # never query into it, and the whole point is that the server hands it to
    # the plugin verbatim.
    payload = Column(Text, nullable=False)
    # Why this section exists / where its contents come from, for whoever finds
    # this table in three months.
    description = Column(String(255), nullable=True)
    # How the section was last produced, e.g. "scripts/build_manifest.py" — a
    # hand-edited row and a generated one behave differently on the next run.
    source = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
