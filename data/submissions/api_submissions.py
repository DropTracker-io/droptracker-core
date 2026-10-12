"""Intake side of the submission API (suggestion #198).

``POST /v2/submissions`` (data_api/routes/submissions.py) authenticates a
submit-capable group key, checks the player is in that group, validates the
screenshot, then forwards the submission to the intake's ``/manual-submit``
with the ``api_*`` fields below. Everything that writes lives here, because
the data API's DB user is SELECT-only.

What this module adds on top of an ordinary manual submission:

* **Idempotency.** One ``api_submissions`` row per call, claimed before any
  processing. ``external_id`` is unique per key, so a bot's retry gets the
  first call's answer back instead of crediting twice.
* **Already tracked by the plugin** (``skip_if_tracked``, on by default). A
  drop the plugin already recorded is not recorded again. The check is
  count-aware: each plugin drop can stand in for ONE API submission
  (``matched_drop_id`` is unique), so two whips submitted against one tracked
  whip skip the first and record the second.
* **Pre-approval.** A ``submit_preapproved`` key's group already reviewed the
  submission, so the payload carries the ``_api_trusted_*`` markers that
  data.submissions.common reads: that group's manual-submission policy counts
  it as an authorized member's, and events the group hosts or takes part in
  treat it as plugin traffic. Other groups apply their own policies as usual.

The decision helpers are pure (unit-tested); the session-bound ones take the
caller's session and never commit on its behalf except where stated.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from .common import API_TRUSTED_EVENTS_KEY, API_TRUSTED_GROUPS_KEY

#: Without an ``obtained_at``, a plugin drop this recent counts as "the same
#: drop". Wide enough for "staff accepted it this evening", narrow enough that
#: yesterday's tracked whip does not swallow today's untracked one.
DEFAULT_TRACKED_WINDOW = timedelta(hours=12)
#: With an ``obtained_at``: a plugin drop within this much either side.
OBTAINED_AT_SLACK = timedelta(minutes=30)
#: How far in the future an ``obtained_at`` may be (clock skew) before it is
#: treated as nonsense and ignored.
_FUTURE_SLACK = timedelta(minutes=10)

#: Outcomes stored on api_submissions.status.
STATUSES = ("processing", "accepted", "rejected", "already_tracked", "error")

NOTE_MAX = 500
EXTERNAL_ID_MAX = 64


# ── pure helpers ──────────────────────────────────────────────────────────────

def _truthy(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_api_context(data: dict) -> Optional[dict]:
    """The submission-API fields of a ``/manual-submit`` body, or None when
    the call did not come from the submission API (website, ``/submit``).

    Multipart bodies arrive with digit-only strings already turned into ints
    by the intake's form parser, so text fields are coerced back to ``str``.
    """
    raw_key = data.get("api_key_id")
    if raw_key in (None, "") or isinstance(raw_key, bool):
        return None
    try:
        key_id = int(raw_key)
        group_id = int(data.get("api_group_id"))
    except (TypeError, ValueError):
        return None
    external_id = data.get("api_external_id")
    external_id = str(external_id).strip()[:EXTERNAL_ID_MAX] if external_id not in (None, "") else None
    note = data.get("api_note")
    note = str(note).strip()[:NOTE_MAX] if note not in (None, "") else None
    obtained_at = None
    raw_ts = data.get("api_obtained_at")
    if raw_ts not in (None, "") and not isinstance(raw_ts, bool):
        try:
            obtained_at = datetime.utcfromtimestamp(int(raw_ts))
        except (TypeError, ValueError, OverflowError, OSError):
            obtained_at = None
    return {
        "key_id": key_id,
        "group_id": group_id,
        "preapproved": _truthy(data.get("api_preapproved"), False),
        "external_id": external_id or None,
        "note": note or None,
        "skip_if_tracked": _truthy(data.get("api_skip_if_tracked"), True),
        "obtained_at": obtained_at,
    }


def tracked_window(obtained_at: Optional[datetime], now: datetime):
    """``(start, end)`` (UTC, naive) a plugin drop must fall inside to count as
    the drop being submitted. ``obtained_at`` narrows it to ±30 minutes; one
    in the future past clock skew is ignored."""
    if obtained_at is not None and obtained_at <= now + _FUTURE_SLACK:
        return obtained_at - OBTAINED_AT_SLACK, obtained_at + OBTAINED_AT_SLACK
    return now - DEFAULT_TRACKED_WINDOW, now


def pick_unmatched(plugin_drop_ids: Iterable[int], already_matched: Iterable[int]) -> Optional[int]:
    """The oldest plugin drop no earlier API submission was matched to, or
    None — every tracked copy is already spoken for, so this one is new."""
    taken = {int(d) for d in already_matched}
    for drop_id in sorted(int(d) for d in plugin_drop_ids):
        if drop_id not in taken:
            return drop_id
    return None


def apply_trust(processed_data: dict, ctx: dict, event_ids: Iterable[int]) -> None:
    """Stamp the pre-approval markers onto the processor payload."""
    if not ctx or not ctx.get("preapproved"):
        return
    processed_data[API_TRUSTED_GROUPS_KEY] = [int(ctx["group_id"])]
    processed_data[API_TRUSTED_EVENTS_KEY] = sorted({int(e) for e in event_ids})


def outcome_body(row, replayed: bool = False) -> dict:
    """The JSON the intake returns for an API submission (and replays)."""
    status = getattr(row, "status", None) or "error"
    body = {
        "success": status in ("accepted", "already_tracked"),
        "status": status,
        "message": getattr(row, "message", None),
        "api_submission_id": int(row.id) if getattr(row, "id", None) is not None else None,
        "guid": getattr(row, "submission_guid", None),
        "matched_drop_id": getattr(row, "matched_drop_id", None),
    }
    if replayed:
        body["replayed"] = True
    return body


# ── session-bound helpers ─────────────────────────────────────────────────────

def claim(session, ctx: dict, player_id: int, submission_type: str, item_name):
    """Insert this call's ``api_submissions`` row and COMMIT it, so the claim
    survives a processor failure that rolls the session back.

    Returns ``(row, existing)``: ``existing`` is the earlier row when this
    ``external_id`` was already used by this key (the caller replays it).
    """
    from sqlalchemy.exc import IntegrityError

    from db.models import ApiSubmission

    row = ApiSubmission(
        api_key_id=ctx["key_id"],
        group_id=ctx["group_id"],
        external_id=ctx.get("external_id"),
        player_id=int(player_id),
        submission_type=str(submission_type)[:24],
        item_name=(str(item_name)[:120] if item_name else None),
        status="processing",
        preapproved=bool(ctx.get("preapproved")),
        note=ctx.get("note"),
        obtained_at=ctx.get("obtained_at"),
    )
    session.add(row)
    try:
        session.commit()
        return row, None
    except IntegrityError:
        session.rollback()
        if not ctx.get("external_id"):
            raise
        existing = (
            session.query(ApiSubmission)
            .filter(ApiSubmission.api_key_id == ctx["key_id"],
                    ApiSubmission.external_id == ctx["external_id"])
            .first()
        )
        return None, existing


def find_tracked_drop(session, row, item_name: str, obtained_at, now=None) -> Optional[int]:
    """Match ``row`` to a plugin drop of the same item by the same player in
    the window, and record the match. Returns the drop id, or None when the
    submission is new. Count-aware: drops earlier API submissions were already
    matched to are skipped, and the unique ``matched_drop_id`` index settles a
    race between two concurrent calls (the loser tries the next drop).
    """
    from sqlalchemy import bindparam, text
    from sqlalchemy.exc import IntegrityError

    now = now or datetime.utcnow()
    start, end = tracked_window(obtained_at, now)
    item_ids = [r[0] for r in session.execute(
        text("SELECT item_id FROM items WHERE item_name = :name LIMIT 50"),
        {"name": item_name},
    ).fetchall()]
    if not item_ids:
        return None
    # date_added is the server's accept time in its own clock; compare in the
    # DB's clock too by converting the UTC window there.
    plugin_ids = [r[0] for r in session.execute(
        text(
            "SELECT d.drop_id FROM drops d "
            "WHERE d.player_id = :pid AND d.item_id IN :ids "
            "AND d.date_added BETWEEN CONVERT_TZ(:start, '+00:00', @@session.time_zone) "
            "AND CONVERT_TZ(:end, '+00:00', @@session.time_zone) "
            "AND (d.source IS NULL OR d.source <> 'manual') "
            "ORDER BY d.drop_id LIMIT 50"
        ).bindparams(bindparam("ids", expanding=True)),
        {"pid": int(row.player_id), "ids": item_ids, "start": start, "end": end},
    ).fetchall()]
    if not plugin_ids:
        return None
    for _attempt in range(3):
        matched = [r[0] for r in session.execute(
            text(
                "SELECT matched_drop_id FROM api_submissions "
                "WHERE matched_drop_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": plugin_ids},
        ).fetchall()]
        drop_id = pick_unmatched(plugin_ids, matched)
        if drop_id is None:
            return None
        row.matched_drop_id = drop_id
        try:
            session.commit()
            return drop_id
        except IntegrityError:
            # Another call took this drop between our read and write.
            session.rollback()
            row.matched_drop_id = None
    return None


def trusted_event_ids_for_group(session, group_id: int) -> list:
    """Live or upcoming events the group hosts or has accepted a place in."""
    from sqlalchemy import text

    return [r[0] for r in session.execute(
        text(
            "SELECT e.id FROM web_events e "
            "WHERE e.status IN ('active', 'draft') AND (e.group_id = :gid OR EXISTS ("
            "  SELECT 1 FROM web_event_groups eg WHERE eg.event_id = e.id "
            "  AND eg.group_id = :gid AND eg.status = 'accepted'))"
        ),
        {"gid": int(group_id)},
    ).fetchall()]


def finish(session, row, status: str, message=None, guid=None, image_url=None) -> None:
    """Record the outcome on the claimed row and commit. Never raises: the
    submission itself already happened, and its answer must still go back."""
    try:
        row.status = status if status in STATUSES else "error"
        row.message = (str(message)[:255] if message else None)
        if guid:
            row.submission_guid = str(guid)[:64]
        if image_url:
            row.image_url = str(image_url)[:255]
        session.add(row)
        session.commit()
    except Exception as e:
        print(f"[SubmissionAPI] couldn't record outcome for api_submission {getattr(row, 'id', '?')}: {e}")
        try:
            session.rollback()
        except Exception:
            pass
