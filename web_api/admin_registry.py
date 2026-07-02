"""Task 12 — superadmin data viewer/editor registry (curated, SAFE).

There is deliberately **no** arbitrary-SQL executor (see
``docs/backend-tasks/12-superadmin.md`` §9/§14.1). Instead, the data
viewer/editor exposes a small, explicitly whitelisted set of entities mapping to
ORM models. Each entity declares:

  * ``model``       - the ORM class (imported from ``db``).
  * ``pk``          - primary-key column name.
  * ``pk_type``     - ``"int"`` (default) or ``"str"`` (e.g. subscription tiers).
  * ``columns``     - the columns that may be **viewed** (sensitive columns such
                      as ``users.auth_token`` are intentionally omitted).
  * ``editable``    - a *small* allowlist of columns that may be **edited**.
                      Never includes primary keys or sensitive columns.
  * ``search_text`` - text columns matched with ``ILIKE %q%`` for the ``q`` param.
  * ``search_int``  - integer columns matched by exact id when ``q`` is numeric.

Everything here is pure data + validation so it can be unit-tested without a live
DB. The actual query execution lives in ``web_api/routes/admin.py``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from db import (
    Announcement,
    AuditLog,
    DiscordOutbox,
    Group,
    GroupConfiguration,
    GroupSubscription,
    Log,
    NotificationQueue,
    Player,
    SubscriptionTier,
    User,
)
from web_api.common import abort_problem

# Hard cap on list page size.
MAX_LIMIT = 100

# --------------------------------------------------------------------------- #
# Whitelisted entities. Adding a new entity here (and only here) is the single
# place that grants the data viewer access to a table.
# --------------------------------------------------------------------------- #
ENTITY_REGISTRY: Dict[str, Dict[str, Any]] = {
    "players": {
        "model": Player,
        "pk": "player_id",
        "columns": [
            "player_id", "player_name", "wom_id", "user_id",
            "total_level", "log_slots", "hidden", "date_added", "date_updated",
        ],
        "editable": ["player_name", "total_level", "hidden"],
        "search_text": ["player_name"],
        "search_int": ["player_id", "wom_id"],
    },
    "groups": {
        "model": Group,
        "pk": "group_id",
        "columns": [
            "group_id", "group_name", "description", "wom_id", "guild_id",
            "invite_url", "icon_url", "date_added", "date_updated",
        ],
        "editable": ["group_name", "description", "invite_url", "icon_url"],
        "search_text": ["group_name"],
        "search_int": ["group_id", "wom_id"],
    },
    "users": {
        "model": User,
        "pk": "user_id",
        # NOTE: auth_token is intentionally excluded (sensitive credential).
        "columns": [
            "user_id", "discord_id", "username", "xf_user_id", "public",
            "global_ping", "group_ping", "never_ping", "hidden",
            "is_superadmin", "patreon_group", "premium_group", "date_added",
        ],
        "editable": ["username", "public", "hidden", "is_superadmin"],
        "search_text": ["username", "discord_id"],
        "search_int": ["user_id"],
    },
    "group_configurations": {
        "model": GroupConfiguration,
        "pk": "id",
        "columns": [
            "id", "group_id", "config_key", "config_value", "long_value",
            "updated_at",
        ],
        "editable": ["config_value", "long_value"],
        "search_text": ["config_key"],
        "search_int": ["id", "group_id"],
    },
    "subscription_tiers": {
        "model": SubscriptionTier,
        "pk": "key",
        "pk_type": "str",
        "columns": [
            "key", "name", "description", "price_cents", "currency", "interval",
            "features", "recommended", "provider_price_id", "active",
            "created_at", "updated_at",
        ],
        "editable": [
            "name", "description", "price_cents", "currency", "interval",
            "recommended", "active",
        ],
        "search_text": ["key", "name"],
        "search_int": [],
    },
    "group_subscriptions": {
        "model": GroupSubscription,
        "pk": "id",
        "columns": [
            "id", "group_id", "tier_key", "status", "provider",
            "provider_customer_id", "provider_subscription_id",
            "current_period_end", "cancel_at_period_end", "created_at",
            "updated_at",
        ],
        "editable": ["tier_key", "status", "cancel_at_period_end"],
        "search_text": ["status", "tier_key"],
        "search_int": ["id", "group_id"],
    },
    "audit_log": {
        "model": AuditLog,
        "pk": "id",
        "columns": [
            "id", "actor_user_id", "group_id", "action", "target", "before",
            "after", "created_at",
        ],
        "editable": [],  # audit trail is read-only.
        "search_text": ["action", "target"],
        "search_int": ["id", "actor_user_id", "group_id"],
    },
    "announcements": {
        "model": Announcement,
        "pk": "id",
        "columns": [
            "id", "scope_type", "group_id", "author_user_id", "title",
            "body_md", "cover_image_url", "pinned", "status", "published_at",
            "created_at", "updated_at",
        ],
        "editable": ["title", "body_md", "pinned", "status", "cover_image_url"],
        "search_text": ["title", "scope_type"],
        "search_int": ["id", "group_id"],
    },
    "notification_queue": {
        "model": NotificationQueue,
        "pk": "id",
        "columns": [
            "id", "notification_type", "player_id", "group_id", "status",
            "data", "error_message", "created_at", "processed_at",
        ],
        "editable": ["status"],
        "search_text": ["notification_type", "status"],
        "search_int": ["id", "player_id", "group_id"],
    },
    "discord_outbox": {
        "model": DiscordOutbox,
        "pk": "id",
        "columns": [
            "id", "kind", "channel_id", "content", "ref_type", "ref_id",
            "status", "discord_message_id", "error", "actor_user_id",
            "created_at", "processed_at",
        ],
        "editable": ["status"],
        "search_text": ["kind", "status", "channel_id"],
        "search_int": ["id", "ref_id", "actor_user_id"],
    },
}


def list_entities() -> List[str]:
    return sorted(ENTITY_REGISTRY.keys())


def get_spec(entity: str) -> Dict[str, Any]:
    """Return the registry spec for ``entity`` or abort 404 (not whitelisted)."""
    spec = ENTITY_REGISTRY.get(entity)
    if spec is None:
        abort_problem(404, "Unknown entity", f"'{entity}' is not a viewable entity.")
    return spec


def coerce_pk(spec: Dict[str, Any], raw: Any) -> Any:
    """Coerce a URL id param to the entity's primary-key type."""
    if spec.get("pk_type") == "str":
        return str(raw)
    try:
        return int(raw)
    except (ValueError, TypeError):
        abort_problem(422, "Invalid id", "This entity's id must be an integer.")


def validate_editable_fields(entity: str, fields: Any) -> Dict[str, Any]:
    """Validate a PATCH body against the entity's editable allowlist.

    Rejects unknown entities (404), non-object/empty bodies and any field that is
    not explicitly editable (422). Primary keys and sensitive columns are never
    editable because they are absent from the ``editable`` allowlist.
    """
    spec = get_spec(entity)
    if not isinstance(fields, dict) or not fields:
        abort_problem(422, "Invalid fields", "'fields' must be a non-empty object.")
    editable = set(spec["editable"])
    if not editable:
        abort_problem(422, "Read-only entity", f"'{entity}' cannot be edited.")
    unknown = sorted(k for k in fields if k not in editable)
    if unknown:
        abort_problem(
            422,
            "Uneditable field(s)",
            f"Not editable on '{entity}': {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(editable))}.",
        )
    return {k: fields[k] for k in fields}


def serialize_value(v: Any) -> Any:
    """JSON-safe scalar conversion. Datetimes → unix seconds, dates → ISO."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, datetime):
        return int(v.timestamp())
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", "ignore")
        except Exception:
            return str(v)
    if isinstance(v, Decimal):
        return int(v)
    return str(v)


def serialize_row(spec: Dict[str, Any], row: Any) -> Dict[str, Any]:
    return {col: serialize_value(getattr(row, col, None)) for col in spec["columns"]}


def build_comped_grant(tier_key: Any, days: Any, now: datetime | None = None) -> Dict[str, Any]:
    """Validate + compute the fields for a manual (comped) subscription grant.

    Pure logic (no DB): the caller upserts these onto ``group_subscriptions``.
    """
    from datetime import timedelta

    if not tier_key or not isinstance(tier_key, str):
        abort_problem(422, "Missing tier_key", "'tier_key' is required.")
    # bool is a subclass of int — reject it explicitly.
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0 or days > 3650:
        abort_problem(422, "Invalid days", "'days' must be an integer between 1 and 3650.")
    now = now or datetime.now()
    return {
        "provider": "manual",
        "status": "active",
        "tier_key": tier_key,
        "current_period_end": now + timedelta(days=int(days)),
        "cancel_at_period_end": False,
    }
