"""Group mini-sites (``{sub}.SITES_DOMAIN``) — sites-v1.

One claimed site per group, multiple pages per site. Same dedicated-table
shape as ``group_embeds``: the builder API writes here, the public renderer
reads only published columns, and the ``custom_site`` entitlement double-gate
(write-gate in the API, render-gate in the projection) means a saved site
never serves after a subscription lapse.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.orm import relationship

from .base import Base


class GroupSite(Base):
    __tablename__ = "group_sites"
    __table_args__ = (
        UniqueConstraint("group_id", name="uq_group_sites_group"),
        UniqueConstraint("subdomain", name="uq_group_sites_subdomain"),
        {"extend_existing": True},
    )

    site_id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    # Claimed-unique DNS label. groups.canonical_slug is deliberately NOT
    # reused: it is nullable on name collisions and never validated as a
    # hostname label.
    subdomain = Column(String(32), nullable=False)
    theme_key = Column(String(32), nullable=False, default="dusk")
    palette = Column(LONGTEXT, nullable=True)  # JSON {"--dt-gold": "#ffb83f", ...}
    nav = Column(LONGTEXT, nullable=True)  # JSON [{"page_slug"|"href", "label"}, ...]
    # Custom CSS: *_source is what the editor round-trips; custom_css is the
    # tinycss2-validated, #site-root-scoped output and the only column the
    # public renderer ever serves.
    custom_css = Column(MEDIUMTEXT, nullable=True)
    custom_css_source = Column(MEDIUMTEXT, nullable=True)
    published = Column(Boolean, nullable=False, default=False)
    # First publish containing raw HTML/CSS sets needs_review; the site serves
    # immediately but carries noindex until a superadmin clears the flag.
    needs_review = Column(Boolean, nullable=False, default=False)
    reviewed_at = Column(DateTime, nullable=True)
    # Moderation kill switch — set: the tenant host serves a branded 410 page.
    suspended_at = Column(DateTime, nullable=True)
    suspended_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    suspend_reason = Column(String(255), nullable=True)
    # Hosted-content ToS acceptance recorded at claim time.
    tos_version = Column(String(16), nullable=True)
    tos_accepted_at = Column(DateTime, nullable=True)
    tos_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    published_at = Column(DateTime, nullable=True)

    pages = relationship(
        "GroupSitePage",
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="GroupSitePage.position",
    )


class GroupSitePage(Base):
    __tablename__ = "group_site_pages"
    __table_args__ = (
        UniqueConstraint("site_id", "slug", name="uq_site_page_slug"),
        {"extend_existing": True},
    )

    page_id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(
        Integer, ForeignKey("group_sites.site_id", ondelete="CASCADE"), nullable=False
    )
    # 'home' is auto-created at claim, locked, and maps to '/'.
    slug = Column(String(40), nullable=False)
    title = Column(String(80), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    # Draft/published are two columns, copy-on-publish: the editor only ever
    # writes draft_blocks; the public renderer only ever reads published_blocks.
    draft_blocks = Column(LONGTEXT, nullable=False)
    published_blocks = Column(LONGTEXT, nullable=True)
    # Page-scoped stylesheet, same contract as the site-level pair: *_source
    # round-trips in the editor, custom_css is the validated + scoped output
    # and the only column ever served.
    custom_css = Column(MEDIUMTEXT, nullable=True)
    custom_css_source = Column(MEDIUMTEXT, nullable=True)
    schema_version = Column(Integer, nullable=False, default=1)
    published = Column(Boolean, nullable=False, default=False)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    published_at = Column(DateTime, nullable=True)

    site = relationship("GroupSite", back_populates="pages")
