"""CLI for the internal dev tracker (projects → tasks → subtasks + notes).

This is the **agent-facing** write path for the owner's project/task board:
codebase agents on this box have DB access but no Discord-OAuth session, so
they use this instead of the superadmin UI at ``/admin/projects`` (both write
the same ``dev_*`` tables — see db/models/dev_tracker.py and
web_api/routes/dev_tracker.py).

Unlike the maintenance scripts in this directory, this is an interactive CRUD
tool — writes are the point, so there is no dry-run mode. The only guarded
operation is ``delete`` (requires ``--yes``).

Usage (always via the venv):
    ./venv/bin/python -m scripts.project_tracker list [--all] [--json]
    ./venv/bin/python -m scripts.project_tracker show <project> [--json]
    ./venv/bin/python -m scripts.project_tracker add-project "Name" [--desc D]
    ./venv/bin/python -m scripts.project_tracker add-task <project> "Title" [--body B] [--status S]
    ./venv/bin/python -m scripts.project_tracker add-subtask <task_id> "Title"
    ./venv/bin/python -m scripts.project_tracker add-note "Body" --project <p> | --task <task_id>
    ./venv/bin/python -m scripts.project_tracker task-status <task_id> done --note "shipped in abc123"
    ./venv/bin/python -m scripts.project_tracker project-status <project> completed --note "..."
    ./venv/bin/python -m scripts.project_tracker check <subtask_id> [--note N] / uncheck <subtask_id>
    ./venv/bin/python -m scripts.project_tracker edit-project <project> [--name N] [--desc D]
    ./venv/bin/python -m scripts.project_tracker edit-task <task_id> [--title T] [--body B]
    ./venv/bin/python -m scripts.project_tracker delete {project|task|subtask|note} <id> --yes

``<project>`` accepts a numeric id or a (case-insensitive, unambiguous) name
fragment. ``--author`` defaults to "agent" — set it to something more useful
(e.g. "claude:loot-sweep-session") when it helps the owner trace who wrote what.
``--json`` prints machine-readable output for agent consumption.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models.base import session  # noqa: E402
from db.models.dev_tracker import (  # noqa: E402
    PROJECT_STATUSES,
    TASK_STATUSES,
    DevNote,
    DevProject,
    DevSubtask,
    DevTask,
)

STATUS_GLYPHS = {"planned": "·", "in_progress": "→", "blocked": "!", "done": "✓"}


def fail(msg: str) -> "NoReturn":  # noqa: F821 - py3.11, keep annotation lazy
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def resolve_project(ref: str) -> DevProject:
    """Project by id, exact name, or unambiguous name fragment."""
    if str(ref).isdigit():
        p = session.query(DevProject).filter(DevProject.id == int(ref)).first()
        if not p:
            fail(f"no project #{ref}")
        return p
    exact = (
        session.query(DevProject)
        .filter(DevProject.name.ilike(str(ref)))
        .all()
    )
    matches = exact or (
        session.query(DevProject)
        .filter(DevProject.name.ilike(f"%{ref}%"))
        .all()
    )
    if not matches:
        fail(f"no project matching '{ref}' (try `list --all`)")
    if len(matches) > 1:
        opts = ", ".join(f"#{p.id} {p.name!r}" for p in matches)
        fail(f"'{ref}' is ambiguous: {opts}")
    return matches[0]


def get_or_fail(model, row_id: int, label: str):
    row = session.query(model).filter(model.id == int(row_id)).first()
    if not row:
        fail(f"no {label} #{row_id}")
    return row


def touch_project(project_id: int) -> None:
    session.query(DevProject).filter(DevProject.id == project_id).update(
        {"updated_at": datetime.now()}, synchronize_session=False
    )


# ---------------------------------------------------------------------------
# Serialization (mirrors web_api/routes/dev_tracker.py shapes)
# ---------------------------------------------------------------------------

def _iso(dt):
    return dt.isoformat() if dt else None


def subtask_dict(st: DevSubtask) -> dict:
    return {"id": st.id, "task_id": st.task_id, "title": st.title,
            "done": bool(st.done), "note": st.note, "order": st.order,
            "created_at": _iso(st.created_at), "completed_at": _iso(st.completed_at)}


def note_dict(n: DevNote) -> dict:
    return {"id": n.id, "project_id": n.project_id, "task_id": n.task_id,
            "body_md": n.body_md, "author": n.author,
            "created_at": _iso(n.created_at), "updated_at": _iso(n.updated_at)}


def task_dict(t: DevTask) -> dict:
    return {"id": t.id, "project_id": t.project_id, "title": t.title,
            "body_md": t.body_md, "status": t.status,
            "completion_note": t.completion_note, "order": t.order,
            "author": t.author, "created_at": _iso(t.created_at),
            "updated_at": _iso(t.updated_at), "completed_at": _iso(t.completed_at),
            "subtasks": [subtask_dict(s) for s in t.subtasks],
            "notes": [note_dict(n) for n in t.notes]}


def project_dict(p: DevProject, *, deep: bool = True) -> dict:
    out = {"id": p.id, "name": p.name, "description": p.description,
           "status": p.status, "completion_note": p.completion_note,
           "order": p.order, "author": p.author, "created_at": _iso(p.created_at),
           "updated_at": _iso(p.updated_at), "completed_at": _iso(p.completed_at)}
    if deep:
        out["tasks"] = [task_dict(t) for t in p.tasks]
        out["notes"] = [note_dict(n) for n in p.notes if n.task_id is None]
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def print_project_line(p: DevProject) -> None:
    tasks = list(p.tasks)
    done = sum(1 for t in tasks if t.status == "done")
    extra = f" [{p.status}]" if p.status != "active" else ""
    print(f"#{p.id:<4} {p.name}{extra} — {done}/{len(tasks)} tasks done"
          f" (updated {p.updated_at:%Y-%m-%d %H:%M})")


def print_project_tree(p: DevProject) -> None:
    print(f"#{p.id} {p.name} [{p.status}]"
          + (f" — completed {p.completed_at:%Y-%m-%d}" if p.completed_at else ""))
    if p.author:
        print(f"  created {p.created_at:%Y-%m-%d} by {p.author}")
    if p.description:
        print(f"  {p.description}")
    if p.completion_note:
        print(f"  completion note: {p.completion_note}")
    project_notes = [n for n in p.notes if n.task_id is None]
    if project_notes:
        print("  notes:")
        for n in project_notes:
            print(f"    (n{n.id}, {n.author or '?'}, {n.created_at:%m-%d}) {n.body_md}")
    if not p.tasks:
        print("  (no tasks)")
    for t in p.tasks:
        glyph = STATUS_GLYPHS.get(t.status, "?")
        subs = list(t.subtasks)
        progress = f"  {sum(1 for s in subs if s.done)}/{len(subs)}" if subs else ""
        print(f"  [{glyph}] t{t.id} {t.title} ({t.status}){progress}")
        if t.body_md:
            for line in t.body_md.splitlines():
                print(f"        {line}")
        if t.completion_note:
            print(f"        completion note: {t.completion_note}")
        for s in subs:
            box = "x" if s.done else " "
            note = f"  ({s.note})" if s.note else ""
            print(f"      [{box}] s{s.id} {s.title}{note}")
        for n in t.notes:
            print(f"      (n{n.id}, {n.author or '?'}, {n.created_at:%m-%d}) {n.body_md}")


def emit(payload, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        human()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args) -> None:
    q = session.query(DevProject).order_by(DevProject.order.asc(), DevProject.updated_at.desc())
    projects = q.all()
    if not args.all:
        projects = [p for p in projects if p.status == "active"]
    emit([project_dict(p, deep=False) | {
            "tasks_total": len(p.tasks),
            "tasks_done": sum(1 for t in p.tasks if t.status == "done"),
         } for p in projects],
         args.json,
         lambda: ([print_project_line(p) for p in projects]
                  if projects else print(
                      "no projects" if args.all
                      else "no active projects (use --all to include finished ones)")))


def cmd_show(args) -> None:
    p = resolve_project(args.project)
    emit(project_dict(p), args.json, lambda: print_project_tree(p))


def cmd_add_project(args) -> None:
    p = DevProject(name=args.name.strip(), description=(args.desc or None),
                   author=args.author)
    session.add(p)
    session.commit()
    print(f"created project #{p.id} {p.name!r}")


def cmd_edit_project(args) -> None:
    p = resolve_project(args.project)
    if args.name:
        p.name = args.name.strip()
    if args.desc is not None:
        p.description = args.desc.strip() or None
    session.commit()
    print(f"updated project #{p.id}")


def cmd_project_status(args) -> None:
    p = resolve_project(args.project)
    if args.status not in PROJECT_STATUSES:
        fail(f"status must be one of: {', '.join(PROJECT_STATUSES)}")
    if args.status == "completed" and p.status != "completed":
        p.completed_at = datetime.now()
    elif args.status != "completed" and p.status == "completed":
        p.completed_at = None
    p.status = args.status
    if args.note:
        p.completion_note = args.note
    session.commit()
    print(f"project #{p.id} → {p.status}")


def cmd_add_task(args) -> None:
    p = resolve_project(args.project)
    status = args.status or "planned"
    if status not in TASK_STATUSES:
        fail(f"status must be one of: {', '.join(TASK_STATUSES)}")
    t = DevTask(project_id=p.id, title=args.title.strip(),
                body_md=(args.body or None), status=status, author=args.author,
                completed_at=datetime.now() if status == "done" else None)
    session.add(t)
    touch_project(p.id)
    session.commit()
    print(f"created task t{t.id} in project #{p.id} {p.name!r}")


def cmd_edit_task(args) -> None:
    t = get_or_fail(DevTask, args.task_id, "task")
    if args.title:
        t.title = args.title.strip()
    if args.body is not None:
        t.body_md = args.body.strip() or None
    touch_project(t.project_id)
    session.commit()
    print(f"updated task t{t.id}")


def cmd_task_status(args) -> None:
    t = get_or_fail(DevTask, args.task_id, "task")
    if args.status not in TASK_STATUSES:
        fail(f"status must be one of: {', '.join(TASK_STATUSES)}")
    if args.status == "done" and t.status != "done":
        t.completed_at = datetime.now()
    elif args.status != "done" and t.status == "done":
        t.completed_at = None
    t.status = args.status
    if args.note:
        t.completion_note = args.note
    touch_project(t.project_id)
    session.commit()
    print(f"task t{t.id} → {t.status}")


def cmd_add_subtask(args) -> None:
    t = get_or_fail(DevTask, args.task_id, "task")
    st = DevSubtask(task_id=t.id, title=args.title.strip())
    session.add(st)
    touch_project(t.project_id)
    session.commit()
    print(f"created subtask s{st.id} in task t{t.id}")


def _set_subtask(subtask_id: int, done: bool, note: str | None) -> None:
    st = get_or_fail(DevSubtask, subtask_id, "subtask")
    st.done = done
    st.completed_at = datetime.now() if done else None
    if note:
        st.note = note[:500]
    t = session.query(DevTask).filter(DevTask.id == st.task_id).first()
    if t:
        touch_project(t.project_id)
    session.commit()
    print(f"subtask s{st.id} {'checked' if done else 'unchecked'}")


def cmd_check(args) -> None:
    _set_subtask(args.subtask_id, True, args.note)


def cmd_uncheck(args) -> None:
    _set_subtask(args.subtask_id, False, None)


def cmd_add_note(args) -> None:
    if bool(args.project) == bool(args.task):
        fail("pass exactly one of --project or --task")
    if args.task:
        t = get_or_fail(DevTask, args.task, "task")
        project_id, task_id = t.project_id, t.id
    else:
        project_id, task_id = resolve_project(args.project).id, None
    n = DevNote(project_id=project_id, task_id=task_id,
                body_md=args.body.strip(), author=args.author)
    session.add(n)
    touch_project(project_id)
    session.commit()
    where = f"task t{task_id}" if task_id else f"project #{project_id}"
    print(f"created note n{n.id} on {where}")


def cmd_delete(args) -> None:
    if not args.yes:
        fail("refusing to delete without --yes")
    model, label = {
        "project": (DevProject, "project"),
        "task": (DevTask, "task"),
        "subtask": (DevSubtask, "subtask"),
        "note": (DevNote, "note"),
    }[args.kind]
    row = get_or_fail(model, args.id, label)
    if isinstance(row, DevTask):
        touch_project(row.project_id)
    elif isinstance(row, (DevNote,)):
        touch_project(row.project_id)
    elif isinstance(row, DevSubtask):
        t = session.query(DevTask).filter(DevTask.id == row.task_id).first()
        if t:
            touch_project(t.project_id)
    session.delete(row)
    session.commit()
    print(f"deleted {label} #{args.id}")


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="project_tracker",
        description="Internal dev tracker CLI (see module docstring for examples).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def author_arg(p):
        p.add_argument("--author", default="agent",
                       help="who wrote this (default: agent)")

    p = sub.add_parser("list", help="list projects")
    p.add_argument("--all", action="store_true", help="include completed/archived")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show a project's full tree")
    p.add_argument("project", help="project id or name fragment")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("add-project", help="create a project")
    p.add_argument("name")
    p.add_argument("--desc")
    author_arg(p)
    p.set_defaults(fn=cmd_add_project)

    p = sub.add_parser("edit-project", help="rename / re-describe a project")
    p.add_argument("project")
    p.add_argument("--name")
    p.add_argument("--desc")
    p.set_defaults(fn=cmd_edit_project)

    p = sub.add_parser("project-status", help="set project status (active/completed/archived)")
    p.add_argument("project")
    p.add_argument("status", choices=PROJECT_STATUSES)
    p.add_argument("--note", help="completion note")
    p.set_defaults(fn=cmd_project_status)

    p = sub.add_parser("add-task", help="add a task to a project")
    p.add_argument("project")
    p.add_argument("title")
    p.add_argument("--body", help="markdown details / plan")
    p.add_argument("--status", choices=TASK_STATUSES)
    author_arg(p)
    p.set_defaults(fn=cmd_add_task)

    p = sub.add_parser("edit-task", help="edit a task's title/body")
    p.add_argument("task_id", type=int)
    p.add_argument("--title")
    p.add_argument("--body")
    p.set_defaults(fn=cmd_edit_task)

    p = sub.add_parser("task-status", help="set task status (planned/in_progress/blocked/done)")
    p.add_argument("task_id", type=int)
    p.add_argument("status", choices=TASK_STATUSES)
    p.add_argument("--note", help="completion note")
    p.set_defaults(fn=cmd_task_status)

    p = sub.add_parser("add-subtask", help="add a checklist line to a task")
    p.add_argument("task_id", type=int)
    p.add_argument("title")
    p.set_defaults(fn=cmd_add_subtask)

    p = sub.add_parser("check", help="mark a subtask done")
    p.add_argument("subtask_id", type=int)
    p.add_argument("--note", help="short note on the line item")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("uncheck", help="mark a subtask not done")
    p.add_argument("subtask_id", type=int)
    p.set_defaults(fn=cmd_uncheck)

    p = sub.add_parser("add-note", help="attach a note to a project or task")
    p.add_argument("body")
    p.add_argument("--project", help="project id or name fragment")
    p.add_argument("--task", type=int, help="task id")
    author_arg(p)
    p.set_defaults(fn=cmd_add_note)

    p = sub.add_parser("delete", help="delete a row (requires --yes)")
    p.add_argument("kind", choices=("project", "task", "subtask", "note"))
    p.add_argument("id", type=int)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_delete)

    return ap


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    finally:
        session.remove()


if __name__ == "__main__":
    main()
