"""External data API (v2) key ORM models.

Two tables:
    ApiKeyTier - named limit bundles ('standard', 'elevated', 'partner').
    ApiKey     - one bearer credential, owned by a user OR a group.

Design decisions (owner, 2026-08-27, dev-tracker #15):
    * A key's tier is a property of the KEY, not of any subscription.
      Every key starts on the lowest tier — premium groups included — and an
      admin promotes it once its usage is verified non-abusive. Subscription
      entitlements gate who may *mint* a user key, never what limits it gets.
    * Per-key nullable overrides beat the tier value where set, so the ACP
      can hand-craft limits for one consumer without inventing a tier.

The secret is stored as a SHA-256 hex digest and shown exactly once at mint.
The public token embeds the row id (``dtk_<id>_<secret>``) so lookup is a
primary-key fetch followed by a constant-time hash comparison — never a scan.
Pure mint/parse/verify/limit logic lives in ``db/api_keys.py``; these classes
are only the storage shape.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)

from .base import Base


class ApiKeyTier(Base):
    """A named bundle of limits a key can be promoted into."""

    __tablename__ = "api_key_tiers"

    tier_key = Column(String(32), primary_key=True)
    display_name = Column(String(64), nullable=False)
    #: Requests accepted per rolling minute.
    requests_per_min = Column(Integer, nullable=False)
    #: Cost units per rolling minute — each include-section carries a weight;
    #: a request costs players x sum(weights).
    cost_units_per_min = Column(Integer, nullable=False)
    #: Requests accepted per rolling day.
    requests_per_day = Column(Integer, nullable=False)
    #: Concurrent in-flight requests allowed.
    max_concurrency = Column(Integer, nullable=False)
    #: Disabled tiers cannot be assigned but keep their history.
    enabled = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)


class ApiKey(Base):
    """One bearer credential for the /v2 data API."""

    __tablename__ = "api_keys"
    __table_args__ = (
        # Scope and ownership must agree, and `global` must be *stated*.
        #
        # The obvious encoding — "neither owner set means it can read
        # everything" — makes an all-access key the result of forgetting to
        # set an owner. Any bug on the mint path would silently produce one.
        # Requiring scope='global' explicitly means the same bug violates this
        # constraint and the insert fails instead.
        CheckConstraint(
            "(scope = 'user'   AND owner_user_id IS NOT NULL AND group_id IS NULL)"
            " OR (scope = 'group'  AND group_id IS NOT NULL AND owner_user_id IS NULL)"
            " OR (scope = 'global' AND owner_user_id IS NULL AND group_id IS NULL)",
            name="ck_api_keys_scope_owner",
        ),
        Index("idx_api_keys_owner_user", "owner_user_id"),
        Index("idx_api_keys_group", "group_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    #: SHA-256 hex of the secret half of the token. The plaintext is never stored.
    token_hash = Column(String(64), nullable=False)
    #: First characters of the secret, for "dtk_12_ab34cd…" display in lists.
    token_prefix = Column(String(8), nullable=False)
    label = Column(String(64), nullable=False, default="")

    #: 'user' | 'group' | 'global'. A global key reads every group and every
    #: player — for third-party integrations — and is staff-issued only.
    #: Visibility is NOT part of scope: a hidden player stays invisible to a
    #: global key, exactly as they are on the website.
    scope = Column(String(16), nullable=False, default="group")
    owner_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)

    tier_key = Column(
        String(32), ForeignKey("api_key_tiers.tier_key"), nullable=False,
        default="standard",
    )
    #: Nullable per-key overrides; where set they beat the tier's value.
    requests_per_min = Column(Integer, nullable=True)
    cost_units_per_min = Column(Integer, nullable=True)
    requests_per_day = Column(Integer, nullable=True)
    max_concurrency = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    #: Who minted it (admin or self-serve); NULL for scripts.
    created_by_user_id = Column(Integer, nullable=True)
    #: Throttled touch (at most ~1/min) — a liveness hint, not an audit row.
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    #: Admin-facing free text ("promoted 2026-09: verified batch client").
    notes = Column(Text, nullable=True)
