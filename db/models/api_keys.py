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

    #: May this key call ``POST /v2/submissions`` (suggestion #198)? Group
    #: keys only, staff-granted. Off for every key by default — read access
    #: never implies write access.
    can_submit = Column(Boolean, nullable=False, default=False, server_default="0")
    #: The group already reviewed what this key sends: its submissions count
    #: as authorized for the key's group (no manual-submission review) and as
    #: plugin traffic on events that group hosts or takes part in. Explicit
    #: per-task / per-event "requires confirmation" still holds them.
    submit_preapproved = Column(Boolean, nullable=False, default=False, server_default="0")


class ApiSubmission(Base):
    """One ``POST /v2/submissions`` call (suggestion #198).

    Three jobs in one row: the audit trail (which key credited what, and with
    which screenshot), the idempotency record (``external_id`` is unique per
    key, so a caller's retry returns the first answer instead of crediting
    twice), and the count-aware "already tracked by the plugin" check, which
    reads earlier ``already_tracked`` rows for the same player and item.

    Written by the intake (``api/routes/webhook.py``) — the data API's own DB
    user is SELECT-only, so it only ever reads this table.
    """

    __tablename__ = "api_submissions"
    __table_args__ = (
        Index("uq_api_submission_external", "api_key_id", "external_id", unique=True),
        Index("idx_api_submission_player_item", "player_id", "item_name", "created_at"),
        Index("idx_api_submission_group", "group_id", "id"),
        # One plugin drop stands in for one API submission at most.
        Index("uq_api_submission_matched_drop", "matched_drop_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id = Column(BigInteger, ForeignKey("api_keys.id"), nullable=False)
    group_id = Column(Integer, nullable=False)
    external_id = Column(String(64), nullable=True)
    player_id = Column(Integer, nullable=False)
    submission_type = Column(String(24), nullable=False)
    item_name = Column(String(120), nullable=True)
    #: processing | accepted | rejected | already_tracked | error
    status = Column(String(16), nullable=False, default="processing", server_default="processing")
    message = Column(String(255), nullable=True)
    submission_guid = Column(String(64), nullable=True)
    #: The plugin drop an ``already_tracked`` submission was matched to.
    matched_drop_id = Column(Integer, nullable=True)
    preapproved = Column(Boolean, nullable=False, default=False, server_default="0")
    image_url = Column(String(255), nullable=True)
    note = Column(String(500), nullable=True)
    obtained_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
