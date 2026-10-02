"""Provenance for rows added to the ``items`` and ``npc_list`` catalogues.

Both tables are written from several places (the drop intake, name-based wiki
resolution, the clan-chat sentinel, seed scripts) and neither has a creation
timestamp, so a bad row could not be traced back to where it came from.
Ticket #456 hit two of them: ``Scurrius\\' spine``, from a 2024 import, and
``NPC Reward pool (Tempoross)``, whose source we never found.

:func:`register` attaches ``after_insert`` hooks to both models. Every new row
then writes an ``audit_log`` entry (``catalog.item.create`` /
``catalog.npc.create``) in the same transaction. The entry records the name,
the process, the first application stack frames outside SQLAlchemy, and the
submission that caused it, when a caller has set one with
:func:`catalog_origin`. A failed audit write is logged and swallowed. It must
never cost the catalogue row, or the submission behind it.

Kept free of ``db`` imports so the pure helpers are unit-testable (the test
bootstrap stubs the ``db`` package).
"""

import contextlib
import contextvars
import json
import os
import sys
import traceback
from typing import Optional

# The submission (or script) currently allowed to add catalogue rows.
_origin: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "catalog_origin", default=None)

# Stack frames from these paths are plumbing, not the caller worth recording.
_SKIP_PATH_PARTS = (
    os.sep + "sqlalchemy" + os.sep,
    os.sep + "site-packages" + os.sep,
    os.sep + "asyncio" + os.sep,
    "catalog_audit.py",
)

# Submission fields worth keeping: who sent it, which submission, and the raw
# names it carried (the raw name is what explains a malformed catalogue row).
_CONTEXT_KEYS = (
    "type", "player_name", "guid", "used_api", "source", "world_type",
    "item_name", "item_id", "npc_name", "source_type",
)

_MAX_FRAMES = 6
_MAX_VALUE_LEN = 120


def submission_context(data, path: str) -> dict:
    """The provenance fields of one submission dict, trimmed for storage.

    ``path`` names the intake route (``plugin``, ``manual-submit``, ...)."""
    ctx = {"path": path}
    if not isinstance(data, dict):
        return ctx
    for key in _CONTEXT_KEYS:
        value = data.get(key)
        if value is None and key == "player_name":
            value = data.get("player")
        if value is None or value == "":
            continue
        ctx[key] = value if isinstance(value, (bool, int)) else str(value)[:_MAX_VALUE_LEN]
    return ctx


@contextlib.contextmanager
def catalog_origin(context: dict):
    """Attribute any catalogue rows added inside this block to ``context``."""
    token = _origin.set(dict(context or {}))
    try:
        yield
    finally:
        _origin.reset(token)


def current_origin() -> Optional[dict]:
    return _origin.get()


def annotate_origin(fields: dict) -> None:
    """Add fields to the enclosing :func:`catalog_origin` block, for callers
    that only learn the submission's details after the block was opened.
    No-op outside a block."""
    origin = _origin.get()
    if origin is not None and fields:
        origin.update(fields)


def caller_frames(stack=None, limit: int = _MAX_FRAMES) -> list:
    """``file:line func`` for the innermost application frames, innermost
    first, skipping SQLAlchemy/asyncio/site-packages plumbing."""
    if stack is None:
        stack = traceback.extract_stack()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
    frames = []
    for frame in reversed(stack):
        filename = frame.filename
        if any(part in filename for part in _SKIP_PATH_PARTS):
            continue
        if filename.startswith(root):
            filename = filename[len(root):]
        frames.append(f"{filename}:{frame.lineno} {frame.name}")
        if len(frames) >= limit:
            break
    return frames


def _systemd_unit(cgroup_text: Optional[str] = None) -> Optional[str]:
    """The ``*.service`` this process runs under, from /proc/self/cgroup."""
    if cgroup_text is None:
        try:
            with open("/proc/self/cgroup") as fh:
                cgroup_text = fh.read()
        except OSError:
            return None
    for line in cgroup_text.splitlines():
        for part in reversed(line.rsplit(":", 1)[-1].split("/")):
            if part.endswith(".service"):
                return part
    return None


def process_label() -> str:
    """Which process wrote the row: the systemd unit plus the script that
    was run (a unit alone can't tell a service from a script run by hand
    inside a user session)."""
    argv0 = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "?"
    if argv0 in ("-m", "__main__.py") and len(sys.argv) > 1:
        argv0 = sys.argv[1]
    unit = _systemd_unit()
    label = f"{argv0} (pid {os.getpid()})"
    return f"{unit}: {label}" if unit else label


def build_entry(kind: str, row_id, name, *, stack=None) -> dict:
    """The audit_log columns for one new catalogue row (pure)."""
    payload = {
        "name": name,
        "process": process_label(),
        "caller": caller_frames(stack),
    }
    origin = current_origin()
    if origin:
        payload["origin"] = origin
    table = "items" if kind == "item" else "npc_list"
    return {
        "action": f"catalog.{kind}.create",
        "target": f"{table}.{row_id}"[:128],
        "after": json.dumps(payload, default=str),
    }


def register(item_cls, npc_cls, audit_table) -> None:
    """Attach the after_insert hooks. Idempotent per class."""
    from sqlalchemy import event

    def _hook(kind, id_attr, name_attr):
        def after_insert(mapper, connection, target):
            row_id = getattr(target, id_attr, None)
            name = getattr(target, name_attr, None)
            try:
                entry = build_entry(kind, row_id, name)
                connection.execute(audit_table.insert().values(**entry))
                print(f"[catalog] new {kind} {row_id} {name!r} "
                      f"origin={entry['after']}", flush=True)
            except Exception as e:  # never cost the catalogue row
                print(f"[catalog] audit write failed for {kind} {row_id} {name!r}: {e}",
                      flush=True)
        return after_insert

    for cls, kind, id_attr, name_attr in (
            (item_cls, "item", "item_id", "item_name"),
            (npc_cls, "npc", "npc_id", "npc_name")):
        if getattr(cls, "_catalog_audit_registered", False):
            continue
        event.listen(cls, "after_insert", _hook(kind, id_attr, name_attr))
        cls._catalog_audit_registered = True
