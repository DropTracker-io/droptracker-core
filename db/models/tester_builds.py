"""Tester client builds: who downloaded one, and who ran which plugin version.

The builds themselves are files a private build job publishes to disk
(utils/tester_builds.py reads them). These two tables hold what the site
adds on top:

``plugin_test_downloads``   one row each time a Bug Tester downloads a build
                            from the website (web_api/routes/tester_builds.py).
``player_plugin_versions``  the first and latest time each account submitted
                            with each plugin version
                            (utils/plugin_versions.record_sighting).

Together they show whether a tester who took a build went on to run it.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, func

from .base import Base


class PluginTestDownload(Base):
    """One download of a tester build by one signed-in user."""

    __tablename__ = "plugin_test_downloads"
    __table_args__ = (
        Index("idx_ptd_user_created", "user_id", "created_at"),
        Index("idx_ptd_build", "build_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # CASCADE on both tables: these rows are a record about the user or player,
    # and must never be what stops one being removed.
    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    # The manifest's build id, e.g. "6.0.19-2cd73be-rl1.13.1".
    build_id = Column(String(96), nullable=False)
    # Copied from the manifest at download time: only the newest zips are
    # kept, and a row should still say what it was once its build is gone.
    plugin_version = Column(String(32), nullable=True)
    commit_sha = Column(String(40), nullable=True)
    runelite_version = Column(String(32), nullable=True)
    file_name = Column(String(160), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())


class PlayerPluginVersion(Base):
    """One plugin version one account has submitted with."""

    __tablename__ = "player_plugin_versions"
    __table_args__ = (
        Index("idx_ppv_version_seen", "version", "last_seen"),
        {"extend_existing": True},
    )

    player_id = Column(
        Integer, ForeignKey("players.player_id", ondelete="CASCADE"), primary_key=True
    )
    version = Column(String(32), primary_key=True)
    first_seen = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False)
    # Counted once per claim window (six hours), not once per submission.
    sightings = Column(Integer, nullable=False, default=1)
    # Set on first sight and never updated: the version was ahead of the
    # Plugin Hub release when this account first ran it.
    prerelease = Column(Boolean, nullable=False, default=False)
