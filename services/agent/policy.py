"""Workspaces, capability policy and the control protocol for Discord-driven agents.

Everything that decides *what an agent is allowed to be* lives here so the runner
stays a dumb transport. Four concerns:

1. **Workspaces** — which directory an agent runs in. The CLI auto-discovers
   ``CLAUDE.md`` from its cwd only, and both DropTracker repos have their own
   (``disc/CLAUDE.md`` ~25KB, ``web/CLAUDE.md``), so the cwd choice decides which
   project brief the agent boots with. The sibling repo is granted via
   ``--add-dir`` so cross-repo work still reads fine.

2. **Capabilities** — how much the agent may do without a human in the loop.
   Nothing here is hardcoded: the tool allowlist and permission mode both come
   from the environment, and the shipped defaults are the *conservative* ones
   (see CAPABILITY NOTE below). Widening them is a deployment decision the
   operator makes explicitly in ``.env``, not something baked into source.

3. **Token shaping** — agents keep the full Claude Code system prompt (they need
   the real harness), but drop what a headless infra agent can never use: the
   unauthenticated claude.ai MCP servers (Drive/Gmail/Calendar show up as
   ``needs-auth`` and cost prompt space) and the skill catalogue.

4. **Control protocol** — a headless agent has no way to stop and ask the
   operator a question; ``claude -p`` just ends the turn. So every turn must end
   with one marker line, which :func:`parse_control` reads to decide whether the
   session is blocked on the operator, finished, or wants another turn. The CLI's
   own ``post_turn_summary`` event echoes the marker in ``needs_action``, which
   the runner uses as a fallback when the model forgets to emit one.

CAPABILITY NOTE
---------------
An agent driven from chat runs with no one watching each tool call, so the only
thing standing between a typo and production is this policy. ``AGENT_TOOLS``
ships as a specific allowlist rather than "everything", and ``AGENT_PERMISSION_MODE``
ships as ``acceptEdits`` (file edits proceed; commands still need a rule to
match). Grant more only as far as the work actually requires, and prefer adding
a narrow ``Bash(cmd:*)`` entry to ``AGENT_TOOLS`` over loosening the mode.
"""

import os
import shlex
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Control protocol
# --------------------------------------------------------------------------- #

MARK_NEEDS_INPUT = "<<NEEDS-INPUT>>"
MARK_DONE = "<<DONE>>"
MARK_CONTINUE = "<<CONTINUE>>"

#: Turn outcomes. ``needs_input`` parks the session until the operator replies;
#: ``done`` closes it; ``continue`` auto-nudges (bounded by max_auto_continue);
#: ``none`` means no marker was emitted — treated like ``needs_input`` without a
#: question, i.e. the session waits rather than burning tokens on a guess.
CONTROL_NEEDS_INPUT = "needs_input"
CONTROL_DONE = "done"
CONTROL_CONTINUE = "continue"
CONTROL_NONE = "none"

_MARKERS = (
    (MARK_NEEDS_INPUT, CONTROL_NEEDS_INPUT),
    (MARK_DONE, CONTROL_DONE),
    (MARK_CONTINUE, CONTROL_CONTINUE),
)


def parse_control(text: str) -> tuple[str, str, str]:
    """Split a turn's final text into ``(control, payload, visible_text)``.

    Scans from the bottom for the first marker line — the marker is specified as
    the last line, but models occasionally append a trailing blank or a stray
    sentence, so anything after it is tolerated. The marker line is removed from
    ``visible_text`` because Discord renders the payload separately.
    """
    if not text:
        return CONTROL_NONE, "", ""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        for marker, control in _MARKERS:
            if stripped.startswith(marker):
                payload = stripped[len(marker):].strip()
                visible = "\n".join(lines[:i] + lines[i + 1:]).strip()
                return control, payload, visible
    return CONTROL_NONE, "", text.strip()


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #

_ROOT = os.getenv("AGENT_ROOT", "/store/droptracker")


@dataclass(frozen=True)
class Workspace:
    key: str
    cwd: str
    label: str
    add_dirs: tuple[str, ...] = ()
    #: Repos in scope, surfaced in the operating prompt so the agent knows which
    #: branch it is on before it starts writing.
    repos: tuple[str, ...] = ()


WORKSPACES: dict[str, Workspace] = {
    "disc": Workspace(
        key="disc",
        cwd=f"{_ROOT}/disc",
        label="Python backend — Discord bots, API, DB models, Alembic, services",
        add_dirs=(f"{_ROOT}/web",),
        repos=(f"{_ROOT}/disc",),
    ),
    "web": Workspace(
        key="web",
        cwd=f"{_ROOT}/web",
        label="Next.js website — apps/, packages/, pnpm workspace",
        add_dirs=(f"{_ROOT}/disc",),
        repos=(f"{_ROOT}/web",),
    ),
    "both": Workspace(
        key="both",
        cwd=f"{_ROOT}/disc",
        label="Backend-first, website also in scope (cwd=disc, web readable/writable)",
        add_dirs=(f"{_ROOT}/web",),
        repos=(f"{_ROOT}/disc", f"{_ROOT}/web"),
    ),
}

DEFAULT_WORKSPACE = os.getenv("AGENT_DEFAULT_WORKSPACE", "disc")


def get_workspace(key: str | None) -> Workspace:
    return WORKSPACES.get((key or DEFAULT_WORKSPACE).lower(), WORKSPACES["disc"])


# --------------------------------------------------------------------------- #
# Runtime limits. This all rides the Max subscription rather than metered API
# billing, so the defaults are deliberately conservative about how much an agent
# can spend before the operator is asked anything.
# --------------------------------------------------------------------------- #


@dataclass
class Limits:
    max_sessions: int = int(os.getenv("AGENT_MAX_SESSIONS", "3"))
    #: Wall-clock cap on a single turn. Real refactors run tens of minutes; past
    #: this the turn is abandoned and the session marked errored.
    turn_timeout_s: int = int(os.getenv("AGENT_TURN_TIMEOUT", "1800"))
    #: Session is reaped after this long with no activity.
    idle_timeout_s: int = int(os.getenv("AGENT_IDLE_TIMEOUT", "5400"))
    #: How many times <<CONTINUE>> may auto-advance without operator input.
    max_auto_continue: int = int(os.getenv("AGENT_MAX_AUTO_CONTINUE", "2"))
    #: Seconds between "still working" progress edits in Discord.
    progress_interval_s: int = int(os.getenv("AGENT_PROGRESS_INTERVAL", "20"))


LIMITS = Limits()

DEFAULT_MODEL = os.getenv("AGENT_MODEL", "sonnet")
DEFAULT_EFFORT = os.getenv("AGENT_EFFORT", "medium")
CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH", "claude")

#: Default tool allowlist. Covers reading/searching, editing files, and the
#: specific command families the operator's workflows need (git inspection,
#: running the test suite, Alembic). Deliberately does NOT include bare
#: ``Bash`` — that would make the allowlist meaningless. Override wholesale with
#: AGENT_TOOLS (space-separated, shell-quoted).
_DEFAULT_TOOLS = " ".join([
    "Read", "Glob", "Grep", "Edit", "Write", "TodoWrite", "Task",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git show:*)", "Bash(git branch:*)", "Bash(git add:*)",
    "Bash(pytest:*)", "Bash(python -m pytest:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(rg:*)", "Bash(wc:*)",
    "Bash(systemctl status:*)", "Bash(journalctl:*)",
])

#: Permission mode for dispatched sessions. ``acceptEdits`` lets file edits land
#: without a prompt while still requiring commands to match an AGENT_TOOLS rule.
#: The operator can widen this in .env if their workflow needs it — see the
#: CAPABILITY NOTE in this module's docstring before doing so.
PERMISSION_MODE = os.getenv("AGENT_PERMISSION_MODE", "acceptEdits")


def allowed_tools() -> list[str]:
    """Tool allowlist as argv entries, parsed shell-style so entries containing
    spaces (``Bash(python -m pytest:*)``) survive."""
    return shlex.split(os.getenv("AGENT_TOOLS", _DEFAULT_TOOLS))


# --------------------------------------------------------------------------- #
# Operating prompt
# --------------------------------------------------------------------------- #

_OPERATING_PROMPT = """
# DropTracker infrastructure agent (Discord-driven)

You were launched from the project owner's Discord by the DropTracker admin bot.
You are running on the production host as user `debian`, against real
infrastructure — treat every action as production-affecting.

## Workspace
- cwd: {cwd} — {label}
- Also in scope: {add_dirs}
- Git repos: {repos}
- `/store/droptracker/disc` (Python backend) and `/store/droptracker/web`
  (Next.js) are SEPARATE repos with independent branches and histories. A change
  in one is never automatically reflected in the other.

## Operating rules
- Services are systemd units (`droptracker-*`). Only restart one when the change
  actually requires it, and say which unit you restarted.
- Schema changes go through Alembic in `/store/droptracker/disc` — write a
  migration, never hand-edit tables.
- Read before you write, and match the conventions already in the file. Both
  repos have a CLAUDE.md documenting their patterns; follow it.
- Run the relevant tests or linters after changing code and report the real
  result. Never describe something as passing that you did not run.
- Some actions need a capability you were not granted. When a tool call is
  refused, do not try to route around it (no rewriting a blocked command, no
  shelling out to achieve what a denied tool would have done). Report what you
  needed and use the NEEDS-INPUT marker to ask.
- STOP and ask before anything destructive or irreversible: dropping or
  truncating tables, force-pushing, rewriting history, deleting files you did not
  create, mass data updates, or restarting a service during an incident.

## Answering into Discord
Your text is delivered as a Discord message, so:
- Lead with the outcome. No preamble, no restating the task back.
- Stay under ~1200 characters unless detail was requested. Cite concrete
  locations as `file.py:42`. Use fenced blocks only for code or commands.
- Report what you actually did, including anything that failed or you skipped.

## Control protocol (MANDATORY)
End EVERY turn with exactly one of these as the final line:

- `{needs_input} <your question>` — you need a decision, a credential, a
  confirmation, a capability you lack, or an answer only the operator has. The
  session pauses and your question goes to Discord. Use this instead of guessing,
  and instead of making an assumption you would later have to unwind.
- `{done} <one-line summary of what changed>` — the task is complete. Mention
  anything still left for the operator (deploy, approve, review).
- `{continue} <what you are about to do next>` — more autonomous work remains
  and needs another turn. Use sparingly; only {max_continue} automatic
  continuations happen before the operator is asked.

The line must begin with the marker exactly. Emitting no marker parks the
session waiting on the operator, which wastes their time.
""".strip()


def build_operating_prompt(ws: Workspace) -> str:
    return (
        _OPERATING_PROMPT
        .replace("{cwd}", ws.cwd)
        .replace("{label}", ws.label)
        .replace("{add_dirs}", ", ".join(ws.add_dirs) or "(none)")
        .replace("{repos}", ", ".join(ws.repos) or "(none)")
        .replace("{needs_input}", MARK_NEEDS_INPUT)
        .replace("{done}", MARK_DONE)
        .replace("{continue}", MARK_CONTINUE)
        .replace("{max_continue}", str(LIMITS.max_auto_continue))
    )


def build_cli_args(ws: Workspace, session_id: str, model: str, effort: str, name: str) -> list[str]:
    """Full argv for one agent session (minus the binary itself)."""
    args = [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",                    # required for stream-json under --print
        "--session-id", session_id,     # chosen by us => resumable in Claude Code
        "--model", model,
        "--effort", effort,
        "-n", name[:60],
        "--permission-mode", PERMISSION_MODE,
        # Token shaping: no MCP servers (the claude.ai ones are unauthenticated
        # on this host and pure prompt overhead), no skill catalogue, and
        # per-machine sections moved out of the system prompt so the cached
        # prefix is shared across sessions.
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--append-system-prompt", build_operating_prompt(ws),
    ]
    tools = allowed_tools()
    if tools:
        args += ["--allowedTools"] + tools
    for d in ws.add_dirs:
        args += ["--add-dir", d]
    return args
