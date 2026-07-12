"""Runtime-editable "item is a component of X, thus worth Y" valuation rules.

Some items are dropped with a 0gp in-game value but are genuinely worth
something because they are a *component* of a tradeable item — e.g. a bludgeon
axon is worth 1/3 of an Abyssal bludgeon, and an Ultor vestige is worth an Ultor
ring minus 3 Chromium ingots. Historically this logic was hard-coded in
``utils/ge_value.py`` (and duplicated in ``/value_mods`` and a GitHub Pages
``valued_items.txt``), so fixing a missing entry meant a code change + a service
restart, and the several copies drifted out of sync.

This table makes those rules data-driven so superadmins can add/edit/remove them
at runtime (see ``web_api/routes/admin.py``) and users can see them on a live
public page. Each row values one target item as a **linear combination** of
other items' live GE prices::

    value = int((flat_bonus + Σ component.quantity × price(component)) / divisor)

If any component cannot be priced, ``fallback_value`` is used (0 ⇒ fall back to
the value the client reported). ``components`` is a JSON array stored as Text,
mirroring the ``SubscriptionTier.features`` / ``entitlements`` idiom, since the
admin form manages the array as a unit and we never query into it.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Text,
    DateTime,
    UniqueConstraint,
)
from sqlalchemy import func

from .base import Base


class ItemValueOverride(Base):
    __tablename__ = "item_value_overrides"
    __table_args__ = (
        UniqueConstraint("item_id", name="uix_item_value_override_item_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Target item. Matched by id first (robust), then by lower-cased name so
    # manual submissions that arrive without an item_id still resolve.
    item_id = Column(Integer, index=True, nullable=True)
    item_name = Column(String(125), index=True, nullable=False)
    # value = int((flat_bonus + Σ component.quantity × price) / divisor)
    divisor = Column(Integer, nullable=False, default=1)
    flat_bonus = Column(Integer, nullable=False, default=0)
    # Used when a component price is unavailable. 0 ⇒ use the client-provided value.
    fallback_value = Column(Integer, nullable=False, default=0)
    # JSON array: [{"item_id": 13280, "item_name": "Abyssal bludgeon", "quantity": 1}, ...]
    components = Column(Text, nullable=True)
    # Human-readable explanation shown on the public docs page (e.g. "1/3 of an
    # Abyssal bludgeon").
    description = Column(String(255), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    author_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
