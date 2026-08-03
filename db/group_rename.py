"""Shared group-rename service (bot + web).

A group's display name lives in **four** places, and until this module existed
only one of them was written when an admin renamed their clan:

  1. ``groups.group_name`` — the canonical column. Every surface that shows a
     clan name reads it: lootboard headers, Discord embeds, leaderboards,
     search, the plugin's ``/load_config``, and the pretty-URL slug
     (``web_api/common.slug_sql_expr``).
  2. ``group_configurations.group_name`` — what the website's settings editor
     ("Group name / Display name of the group") writes. Nothing else reads it,
     so renaming there alone changed nothing visible; 11 live groups had a
     config value their group had never been renamed to.
  3. ``xenforo.dt_player_group.group_name`` — the forum's mirror, previously
     written once at creation (``db/clan_sync.insert_xf_group``) and never
     again.
  4. In-process caches keyed off the name: the group-config cache
     (``utils.group_config``) and the Web API's canonical-slug cache.

Every rename path must go through :func:`rename_group` so all four move
together. Current callers: ``PATCH /api/v1/groups/{id}/config`` (the settings
editor) and the superadmin data browser's ``groups.group_name`` edit.

The function mutates but never commits — it runs inside the caller's
transaction. Call :func:`invalidate_group_name_caches` *after* the commit.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from db.models import Group, GroupConfiguration

# The settings-editor key that mirrors ``groups.group_name``.
GROUP_NAME_CONFIG_KEY = "group_name"

# ``groups.group_name`` is VARCHAR(30) — the same cap group creation enforces
# (api/routes/group_create.py). A longer name would be truncated by MySQL.
MAX_GROUP_NAME_LENGTH = 30


class GroupRenameError(ValueError):
    """A rename that cannot be applied (empty / too long / unknown group)."""


@dataclass(frozen=True)
class GroupRenameResult:
    group_id: int
    before: Optional[str]
    after: str
    #: False when the name was already correct and only the mirrors were healed.
    changed: bool


def normalize_group_name(raw) -> str:
    """Trim and validate a submitted name, or raise ``GroupRenameError``."""
    name = str(raw if raw is not None else "").strip()
    if not name:
        raise GroupRenameError("Group name cannot be empty.")
    if len(name) > MAX_GROUP_NAME_LENGTH:
        raise GroupRenameError(
            f"Group name must be {MAX_GROUP_NAME_LENGTH} characters or fewer."
        )
    return name


def rename_group(s, group_id: int, new_name) -> GroupRenameResult:
    """Rename ``group_id`` everywhere its name is stored.

    Writes the canonical column, the settings-editor config row and the XenForo
    mirror on the caller's session; does not commit. Safe to call with the name
    a group already has — the mirrors are re-synced either way, which is how
    groups that drifted before this existed get healed on their next save.
    """
    name = normalize_group_name(new_name)

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        raise GroupRenameError(f"No group with id {group_id}.")

    before = group.group_name
    group.group_name = name
    _sync_config_row(s, group_id, name)
    _sync_xf_mirror(s, group_id, name)

    return GroupRenameResult(
        group_id=group_id,
        before=before,
        after=name,
        changed=(before or "") != name,
    )


def invalidate_group_name_caches(group_id: int) -> None:
    """Drop the in-process caches that hold a group's name. Call after commit."""
    try:
        import utils.group_config as gc

        gc.invalidate(group_id)
    except Exception:
        pass

    # The Web API caches the pretty-URL slug it derives from the name for five
    # minutes (web_api/common.canonical_slug_for). Look the module up in
    # sys.modules instead of importing it, so bot and worker processes calling
    # this never drag the web stack in.
    common = sys.modules.get("web_api.common")
    if common is not None:
        try:
            common.cache_delete(f"canonslug:group:{group_id}")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Mirrors
# --------------------------------------------------------------------------- #
def _sync_config_row(s, group_id: int, name: str) -> None:
    """Keep the settings editor's ``group_name`` row equal to the column."""
    row = (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == GROUP_NAME_CONFIG_KEY,
        )
        .first()
    )
    if row:
        row.config_value = name
    else:
        s.add(
            GroupConfiguration(
                group_id=group_id,
                config_key=GROUP_NAME_CONFIG_KEY,
                config_value=name,
            )
        )


def _sync_xf_mirror(s, group_id: int, name: str) -> None:
    """Update the forum's copy of the name (``xenforo.dt_player_group``).

    Best-effort inside a SAVEPOINT: the forum is a secondary surface, and a
    missing row or a permissions problem on the ``xenforo`` schema must not
    roll back the rename itself. Groups created before the forum mirror existed
    simply have no row to update.
    """
    try:
        with s.begin_nested():
            s.execute(
                text(
                    "UPDATE xenforo.dt_player_group "
                    "SET group_name = :name, date_updated = :ts "
                    "WHERE group_id = :group_id"
                ),
                {
                    "name": name,
                    "ts": int(datetime.now().timestamp()),
                    "group_id": group_id,
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[group_rename] XenForo mirror update failed for group {group_id}: {exc}")
