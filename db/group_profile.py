"""A group's description: the settings field is a view of ``groups.description``.

Same shape as ``db/group_rename.py`` for names. The website's Description field
wrote only ``group_configurations.group_description``, while the group page,
leaderboards, the plugin's group endpoint and the forum mirror all read the
``groups.description`` column. So an edited description saved, showed up again
in the settings form, and changed nothing anyone could see (found 2026-11-01).

``web_api/routes/config.py`` reads the column for the form and calls
``set_group_description`` on save; it still writes the config row and the
audit entry itself, so the row stays equal to the column.
"""

from __future__ import annotations

from typing import Optional

from db.models import Group

GROUP_DESCRIPTION_CONFIG_KEY = "group_description"

# groups.description is VARCHAR(255).
MAX_GROUP_DESCRIPTION_LENGTH = 255


def normalize_group_description(value: Optional[str]) -> Optional[str]:
    """Trimmed text, or None for blank (the group page hides a missing one)."""
    text = (value or "").strip()
    return text or None


def set_group_description(s, group_id: int, value: Optional[str]) -> Optional[str]:
    """Write ``groups.description`` and return what it held before.

    Does not commit. A missing group is a no-op that returns None; the config
    route has already checked the caller administers the group.
    """
    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        return None
    before = group.description
    group.description = normalize_group_description(value)
    return before
