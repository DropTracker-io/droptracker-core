"""Bot-side group-config writes done right.

The one pre-existing bot-side writer of a registry key
(``/toggle-split-tracking``) upserts the row but never invalidates
``utils.group_config``'s 30-second cache; the correct pattern lives in
``services/clan_log_discord._save_message_id``. This module is that pattern
as a shared service, plus the two things the website's PATCH route does that
a Discord surface must not lose: registry validation via
``web_api.config_registry`` (a pure module — no app context needed) and an
``AuditLog`` row per key.

Sessions are the caller's problem only in that this opens its own
short-lived one per call (the /settings-panel convention).
"""
from __future__ import annotations

import json

from db.models import GroupConfiguration, Session, User
from utils import group_config

# config_value is VARCHAR(255); longer values spill to long_value with an
# empty config_value — mirrors web_api/routes/config.py LONG_VALUE handling.
_MAX_SHORT_VALUE = 255


def validate_updates(updates: dict) -> dict:
    """Coerce/validate {key: raw} against the registry → {key: stored_str}.
    Raises ``web_api.config_registry.ConfigValidationError`` on a bad value,
    ``KeyError`` on an unknown key."""
    from web_api.config_registry import coerce_to_storage

    return {key: coerce_to_storage(key, raw) for key, raw in updates.items()}


def set_group_config(group_id: int, stored: dict, *,
                     actor_discord_id=None,
                     action: str = "config.update.discord") -> None:
    """Upsert already-validated {key: stored_str} rows for one group, write
    an AuditLog row per changed key, commit, and invalidate the read cache."""
    from web_api.config_registry import SENSITIVE_KEYS

    s = Session()
    try:
        actor_user_id = None
        if actor_discord_id is not None:
            row = (s.query(User.user_id)
                   .filter(User.discord_id == str(actor_discord_id)).first())
            actor_user_id = int(row[0]) if row else None
        existing = {
            r.config_key: r
            for r in s.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key.in_(list(stored)))
            .all()
        }
        for key, value in stored.items():
            value = "" if value is None else str(value)
            long_value = None
            if len(value) > _MAX_SHORT_VALUE:
                long_value, value = value, ""
            row = existing.get(key)
            before = None
            if row is not None:
                before = row.long_value if (row.long_value and not row.config_value) else row.config_value
                if (row.config_value or "") == value and (row.long_value or None) == long_value:
                    continue  # no-op — no audit noise
                row.config_value = value
                row.long_value = long_value
            else:
                s.add(GroupConfiguration(group_id=group_id, config_key=key,
                                         config_value=value, long_value=long_value))
            shown_after = long_value if long_value else value
            if key in SENSITIVE_KEYS:
                before, shown_after = "***", "***"
            from db.models.web import AuditLog

            s.add(AuditLog(
                actor_user_id=actor_user_id,
                group_id=group_id,
                action=action,
                target=f"group_configurations.{key}",
                before=json.dumps(before) if before is not None else None,
                after=json.dumps(shown_after),
            ))
        s.commit()
    finally:
        s.close()
    group_config.invalidate(group_id)


def get_group_config_values(group_id: int, keys) -> dict:
    """{key: effective stored string} for the requested keys ('' when
    unset). Reads long_value when config_value spilled."""
    s = Session()
    try:
        rows = (s.query(GroupConfiguration)
                .filter(GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key.in_(list(keys)))
                .all())
        out = {k: "" for k in keys}
        for r in rows:
            out[r.config_key] = (r.long_value
                                 if (r.long_value and not r.config_value)
                                 else (r.config_value or ""))
        return out
    finally:
        s.close()
