"""Tools & communities directory shown on the web /tools page (web146a).

Each listing is one card: a tool or community The DropTracker uses or points
players to. Superadmins add, edit, hide and remove listings and choose who
maintains each one; a maintainer (usually the person who runs that tool) can
edit their own listing's content from the site.

Section keys, field limits and who may edit which field live in
web_api/community_listings.py so the routes and tests share one copy.

Not registered in db/models/__init__.py on purpose: import it from here.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from .base import Base


class CommunityListing(Base):
    __tablename__ = "community_listings"
    __table_args__ = (
        Index("idx_community_listing_order", "section", "sort_order", "id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(64), nullable=False, unique=True)
    name = Column(String(80), nullable=False)
    section = Column(String(24), nullable=False)
    # One or two sentences, always shown on the card.
    summary = Column(String(300), nullable=False)
    # Longer plain text shown when the card is expanded.
    details = Column(Text, nullable=True)
    # Short badge and note on how it fits with The DropTracker (staff voice).
    fit_label = Column(String(40), nullable=True)
    fit_note = Column(String(400), nullable=True)
    # JSON: ["tag", ...] and [{"label": str, "href": str}, ...] (first = primary).
    tags_json = Column(Text, nullable=False, default="[]")
    links_json = Column(Text, nullable=False, default="[]")
    logo_url = Column(String(512), nullable=True)
    accent = Column(String(7), nullable=True)
    sort_order = Column(Integer, nullable=False, default=100)
    visible = Column(Boolean, nullable=False, default=True)
    created_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    maintainers = relationship(
        "CommunityListingMaintainer",
        back_populates="listing",
        cascade="all, delete-orphan",
        order_by="CommunityListingMaintainer.id",
    )


class CommunityListingMaintainer(Base):
    __tablename__ = "community_listing_maintainers"
    __table_args__ = (
        UniqueConstraint("listing_id", "user_id", name="uix_community_listing_maintainer"),
        Index("idx_community_maintainer_user", "user_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    listing_id = Column(
        Integer,
        ForeignKey("community_listings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    added_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    listing = relationship("CommunityListing", back_populates="maintainers")
