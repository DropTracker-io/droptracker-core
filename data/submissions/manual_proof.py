"""
Proof follow-up helpers for the Discord /submit commands.

Manual submissions stay optional-proof on purpose: most /submit traffic is
casual and touches no event at all. The cost falls on event submissions — an
event whose ``submission_policy`` is ``confirm_non_api`` (the default) parks a
non-plugin submission as a **pending** ``EventCompletion`` for an admin to
confirm, and a pending row with no ``proof_url`` is exactly the one an admin
bounces with "resubmit with your screenshot".

This module is the plumbing behind "only ask for a screenshot when it actually
matters":

* :func:`pending_proof_rows` reads back the ledger rows the events worker just
  wrote for this player, so the "does it qualify?" verdict is the engine's own
  matching decision rather than a second copy of it here.
* :func:`stash_awaiting_proof` / :func:`load_awaiting_proof` keep those row ids
  in a short-TTL Redis registry, so a screenshot can be attached **after** the
  fact instead of resubmitting the whole thing.
* :func:`attach_proof_url` writes the screenshot onto the still-pending rows
  (``EventCompletion.proof_url``), which is what the web review card renders.

Everything here takes its session / Redis connection as an argument and does no
Discord work, so the decision path is unit-testable — ``commands/submissions.py``
itself can't be imported under the test stubs (``interactions`` is a MagicMock,
so ``Extension`` subclasses fail at class creation).
"""

import json
import time
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text

# Mirrors services.event_engine.ACTIVE_EVENTS_KEY — the set the events worker
# maintains. Duplicated as a literal (not imported) because importing the
# engine here would drag the whole events stack into the bot's submit path;
# tests/unit/test_submit_proof_followup.py guards the two against drift.
ACTIVE_EVENTS_KEY = "events:active"

AWAITING_PROOF_KEY = "submit:awaiting-proof:{discord_id}"
# Long enough to go find the screenshot, short enough that a forgotten prompt
# can't silently re-target a much later upload.
AWAITING_PROOF_TTL_SECONDS = 30 * 60

# How far back a proofless pending row still counts as "the thing they just
# submitted" (absorbs the events worker's queue latency and any clock skew
# between this process and MySQL's NOW()).
PROOF_PROMPT_LOOKBACK_SECONDS = 180
# `/submit proof` (or a DM upload) with no live registry entry falls back to
# the caller's recent pending rows — an hour covers "I went and took the
# screenshot" without reaching back to yesterday's review queue.
PROOF_ATTACH_LOOKBACK_SECONDS = 60 * 60

# The events worker writes the completion rows asynchronously (queue_submission
# LPUSHes, workers/event_consumer.py applies), so the rows usually appear a
# beat after /submit returns. Poll a few times, stopping at the first hit; this
# runs AFTER the submitter already has their confirmation embed.
PROOF_POLL_DELAYS = (1.0, 1.5, 2.5)

PROOF_MAX_BYTES = 10 * 1024 * 1024  # matches the web form's 10 MB cap
PROOF_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
_PROOF_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# EventCompletion.proof_url is VARCHAR(255).
_PROOF_URL_MAX = 255

_PENDING_SQL = text(
    "SELECT c.id, c.player_id, c.event_id, e.name, t.label, c.created_at "
    "FROM web_event_completions c "
    "JOIN web_events e ON e.id = c.event_id "
    "JOIN web_event_tasks t ON t.id = c.task_id "
    "WHERE c.player_id IN :player_ids AND c.status = 'pending' "
    "  AND c.proof_url IS NULL AND c.created_at >= :since "
    "ORDER BY c.id DESC LIMIT :limit"
).bindparams(bindparam("player_ids", expanding=True))

_ATTACH_SQL = text(
    "UPDATE web_event_completions SET proof_url = :proof_url, updated_at = NOW() "
    "WHERE id IN :completion_ids AND player_id IN :player_ids "
    "  AND status = 'pending' AND proof_url IS NULL"
).bindparams(
    bindparam("completion_ids", expanding=True),
    bindparam("player_ids", expanding=True),
)


def events_active(conn) -> bool:
    """Whether any event is running at all — the O(1) gate that keeps the
    proofless-submission path query-free the rest of the time. Fail-quiet: no
    Redis means no queued submissions either, so there is nothing to prompt
    about."""
    if conn is None:
        return False
    try:
        return bool(conn.exists(ACTIVE_EVENTS_KEY))
    except Exception:
        return False


def pending_proof_rows(session, player_ids, lookback_seconds: int, limit: int = 25) -> list:
    """The caller's event completions that are waiting on an admin AND carry no
    screenshot, newest first.

    One indexed read (``player_id`` is FK-indexed) — no scoring logic is
    repeated here; a row existing IS the engine's verdict that the submission
    qualified and that the event's policy held it for review.
    """
    ids = [int(pid) for pid in (player_ids or []) if pid]
    if not ids:
        return []
    since = datetime.now() - timedelta(seconds=int(lookback_seconds))
    rows = session.execute(
        _PENDING_SQL, {"player_ids": ids, "since": since, "limit": int(limit)}
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "player_id": int(row[1]) if row[1] is not None else None,
            "event_id": int(row[2]) if row[2] is not None else None,
            "event_name": row[3],
            "task_label": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def latest_batch(rows, window_seconds: int = 120) -> list:
    """The newest burst of pending rows.

    One submission routinely fans out into several completions (several tasks,
    or one per team), so the batch — not the single newest row — is what a
    screenshot belongs to. Used only on the fallback path, where no registry
    entry pins the submission: it stops a screenshot taken an hour later from
    being smeared across an unrelated earlier submission as well.
    """
    stamped = [r for r in rows or [] if r.get("created_at")]
    if not stamped:
        return list(rows or [])
    cutoff = max(r["created_at"] for r in stamped) - timedelta(seconds=int(window_seconds))
    return [r for r in stamped if r["created_at"] >= cutoff]


def attach_proof_url(session, completion_ids, player_ids, proof_url: str) -> int:
    """Set ``proof_url`` on the given still-pending rows; returns how many were
    written. The ownership + status filters are re-applied in the UPDATE, so a
    stale registry entry can never write onto someone else's row or onto one an
    admin already actioned."""
    ids = [int(cid) for cid in (completion_ids or []) if cid]
    owners = [int(pid) for pid in (player_ids or []) if pid]
    if not ids or not owners or not proof_url:
        return 0
    result = session.execute(
        _ATTACH_SQL,
        {
            "proof_url": str(proof_url)[:_PROOF_URL_MAX],
            "completion_ids": ids,
            "player_ids": owners,
        },
    )
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


# ── Awaiting-proof registry ───────────────────────────────────────────────────
# Keyed by the submitter alone (the channel rides along in the payload) because
# a follow-up upload does not have to come back to the same place: a DM lands on
# a different channel entirely, and `/submit proof` works from anywhere.


def awaiting_proof_key(discord_id) -> str:
    return AWAITING_PROOF_KEY.format(discord_id=int(discord_id))


def stash_awaiting_proof(conn, discord_id, *, player_id, player_name,
                         completion_ids, channel_id=None, summary=None) -> bool:
    """Remember which pending rows a follow-up screenshot should land on."""
    ids = [int(cid) for cid in (completion_ids or []) if cid]
    if conn is None or not ids:
        return False
    payload = {
        "v": 1,
        "player_id": int(player_id),
        "player_name": player_name,
        "completion_ids": ids,
        "channel_id": str(channel_id) if channel_id else None,
        "summary": summary,
        "ts": int(time.time()),
    }
    try:
        conn.setex(awaiting_proof_key(discord_id), AWAITING_PROOF_TTL_SECONDS,
                   json.dumps(payload))
        return True
    except Exception:
        return False


def load_awaiting_proof(conn, discord_id):
    """The stashed payload, or None when nothing is waiting (or Redis is out)."""
    if conn is None:
        return None
    try:
        raw = conn.get(awaiting_proof_key(discord_id))
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def clear_awaiting_proof(conn, discord_id) -> None:
    if conn is None:
        return
    try:
        conn.delete(awaiting_proof_key(discord_id))
    except Exception:
        pass


# ── Presentation (pure) ───────────────────────────────────────────────────────


def proof_attachment_error(content_type, size) -> str | None:
    """Same gate the ``proof`` option applies, for the follow-up surfaces."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in PROOF_CONTENT_TYPES:
        return "That file type isn't supported — attach a PNG, JPEG, WebP or GIF image."
    if int(size or 0) > PROOF_MAX_BYTES:
        return "Proof screenshots are capped at 10 MB."
    return None


def proof_extension(content_type, filename=None) -> str:
    """File extension to store a proof screenshot under."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = _PROOF_EXTENSIONS.get(ctype)
    if ext:
        return ext
    if filename and "." in filename:
        candidate = filename.rsplit(".", 1)[1].strip().lower()
        if candidate in {"png", "jpg", "jpeg", "webp", "gif"}:
            return "jpg" if candidate == "jpeg" else candidate
    return "png"


def event_names(rows) -> list:
    """Distinct event names across ``rows``, in row order."""
    names = []
    for row in rows or []:
        name = (row.get("event_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def format_names(names, limit: int = 2) -> str:
    """"**A**", "**A** and **B**", "**A**, **B** and 2 others"."""
    bolded = [f"**{n}**" for n in (names or [])]
    if not bolded:
        return "an event"
    if len(bolded) == 1:
        return bolded[0]
    if len(bolded) <= limit:
        return f"{', '.join(bolded[:-1])} and {bolded[-1]}"
    extra = len(bolded) - limit
    return f"{', '.join(bolded[:limit])} and {extra} other{'s' if extra > 1 else ''}"


def proof_prompt_text(summary: str, rows, command_hint: str = "`/submit proof`") -> str:
    """The nudge shown after a proofless submission that landed in a review
    queue. Deliberately never shown for submissions that touch no event."""
    where = format_names(event_names(rows))
    tasks = []
    for row in rows or []:
        label = (row.get("task_label") or "").strip()
        if label and label not in tasks:
            tasks.append(label)
    task_line = f"\nCounted towards: {format_names(tasks)}." if tasks else ""
    return (
        f"{summary} counts towards {where}, so it's now **waiting for an admin "
        f"to confirm it** — and submissions with no screenshot usually get "
        f"rejected.{task_line}\n\n"
        f"**Send the screenshot now and it's added to what you already "
        f"submitted** — you don't have to submit it again:\n"
        f"• run {command_hint} and attach it, or\n"
        f"• just DM me the screenshot\n"
        f"-# Only event submissions held for review are asked for proof."
    )


def attached_summary_text(rows, updated: int) -> str:
    """Confirmation copy once a follow-up screenshot has been written."""
    if updated <= 0:
        return (
            "That submission isn't waiting on proof any more — an admin has "
            "already reviewed it, so the screenshot wasn't attached."
        )
    where = format_names(event_names(rows))
    noun = "submission" if updated == 1 else "submissions"
    return (
        f"Added your screenshot to {updated} pending {noun} in {where}. "
        f"Whoever reviews it will see the proof — no need to submit again."
    )
