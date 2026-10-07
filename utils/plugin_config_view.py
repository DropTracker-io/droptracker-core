"""How a stored plugin settings snapshot is presented to a reader.

Shared by the website's plugin-config pages (web_api/routes/plugin_config.py)
and the data API's ``plugin_config`` section (data_api/sections.py), so the
two cannot drift into describing the same row differently. Timestamps are
left to each caller: the two surfaces format them differently, and existing
consumers of each depend on their own format.

Stdlib only.
"""
from __future__ import annotations

import json

#: ``env`` keys a reader outside the player's own clan does not get. A custom
#: API endpoint can be a private server's address.
PRIVATE_ENV_KEYS = ("custom_api_endpoint",)


def loads(raw):
    """A stored snapshot as a dict, or None when it is missing or unreadable."""
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def flat(snapshot) -> dict:
    """{key: value} across every section of a snapshot's settings."""
    out = {}
    for values in ((snapshot or {}).get("settings") or {}).values():
        if isinstance(values, dict):
            out.update(values)
    return out


def transport(used_api):
    """How the snapshot reached us: "api", "webhook", or None when unknown."""
    return None if used_api is None else ("api" if used_api else "webhook")


def snapshot_fields(row, current=None, redact_private: bool = False) -> dict:
    """The current snapshot's fields, without timestamps.

    ``current`` is the already-parsed ``row.config_json`` when the caller has
    it. ``redact_private`` drops :data:`PRIVATE_ENV_KEYS` from ``env``.
    """
    if current is None:
        current = loads(row.config_json)
    env = dict((current or {}).get("env") or {})
    if redact_private:
        for key in PRIVATE_ENV_KEYS:
            env.pop(key, None)
    return {
        "plugin_version": row.plugin_version,
        "runelite_version": row.runelite_version,
        "transport": transport(row.used_api),
        "settings": (current or {}).get("settings") or {},
        "customized": (current or {}).get("customized") or [],
        "env": env,
    }
