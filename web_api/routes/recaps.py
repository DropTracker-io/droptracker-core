"""Public recap ("Wrapped") reads.

  GET /api/v1/recaps/{scope}/{subject_id}/{period}   one card
  GET /api/v1/recaps/{scope}/{subject_id}            the archive index

Reads are deliberately dumb: every number was computed and frozen by
``services/recap.py`` when the snapshot was written, which is what lets a recap
URL stay valid forever and answer in a single indexed row read.

**Player cards are generated on first view.** Group cards are few (one per clan
per period) and are produced by an ops run, but there are thousands of players
per month and almost none of those cards would ever be looked at — so
pre-generating them all would spend hours of compute to store rows nobody reads.
A player card is instead computed the first time someone opens its page and
stored from then on, which turns a cold page load into a ~1-3 s compute and every
subsequent one into the same single row read as a group card. See
``_generate_player`` for the guards that keeps that from becoming a lever
(closed months only, one compute per subject at a time, and a remembered "no
card here" answer so an ineligible player isn't recomputed on every hit).

A recap is public by construction — it's a shareable artifact, and gating it
would defeat the point. Privacy is enforced where the card is *built*:
``compute_player_month`` refuses outright for a player who has opted out of
public display, and group aggregates exclude hidden members.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import RecapSnapshot
from db.models.recap import RECAP_SCOPES, SCOPE_PLAYER
from web_api.common import abort_problem, db_session, with_cache_headers

recaps_bp = Blueprint("v1_recaps", __name__)

# A settled period's card never changes, so it can be cached hard. The one
# exception is a period regenerated after a backfill (e.g. the NPC rollup gap),
# which is rare and tolerable — an hour of staleness on an archive page.
_CACHE_SECONDS = 3600


# Guards on first-view generation. A cold player card is one ranged rollup read
# per source — 1-3 s measured — so two Redis keys keep that from being a lever
# anyone can pull repeatedly:
#
#   recap:gen:{scope}:{id}:{period}    in-flight lock, so N people opening the
#                                      same fresh link cause ONE compute
#   recap:nocard:{scope}:{id}:{period} remembered miss, so a player below the
#                                      activity floor — or opted out — is not
#                                      recomputed on every page hit
_GEN_LOCK_SECONDS = 120
_MISS_TTL_SECONDS = 6 * 3600
_LOCK_POLL_SECONDS = 0.5
_LOCK_POLL_TRIES = 10


def _valid_scope(scope: str) -> bool:
    return scope in RECAP_SCOPES


class _CorruptPayload(Exception):
    """A stored row that won't parse — a server fault, not a missing page."""


def _payload_from_row(row) -> dict:
    try:
        payload = json.loads(row.payload)
    except (TypeError, ValueError) as e:
        raise _CorruptPayload() from e
    payload["schema_version"] = int(row.schema_version or 1)
    payload["generated_at"] = row.generated_at.isoformat() if row.generated_at else None
    return payload


def _read_snapshot(scope: str, subject_id: int, period: str):
    """The stored card, or None. Raises :class:`_CorruptPayload`."""
    with db_session() as s:
        row = (
            s.query(RecapSnapshot)
            .filter(
                RecapSnapshot.scope == scope,
                RecapSnapshot.subject_id == subject_id,
                RecapSnapshot.period == period,
            )
            .first()
        )
        return _payload_from_row(row) if row else None


def _redis():
    try:
        from web_api.common import _rc

        return _rc()
    except Exception:
        return None


def _generate_player(subject_id: int, period: str):
    """Compute one player's card and store it. None when there is no card to make.

    Runs on a worker thread — everything below is blocking SQLAlchemy and Redis.
    Only closed months are eligible: an annual card folds twelve stored monthly
    rows, and since those are no longer pre-generated the fold has nothing to read
    (a year URL 404s rather than silently reporting a partial year), and a card
    for the month still in progress is the one mistake a recap can't walk back.
    """
    # Lazy import: tests stub `services` as a MagicMock at module scope, so a
    # top-level import here would bind the mock (see tests/conftest.py).
    from services.recap import (
        compute_player_month,
        is_month_period,
        period_closed,
        save_snapshot,
    )

    if not is_month_period(period) or not period_closed(period):
        return None

    with db_session() as s:
        payload = compute_player_month(s, subject_id, period)
        if payload is None:
            return None
        save_snapshot(s, SCOPE_PLAYER, subject_id, period, payload)
        s.commit()

    # Re-read so the response carries the row's own generated_at/schema_version
    # rather than a second, slightly different rendering of the same facts.
    return _read_snapshot(SCOPE_PLAYER, subject_id, period)


@recaps_bp.get("/recaps/<scope>/<int:subject_id>/<period>")
async def get_recap(scope: str, subject_id: int, period: str):
    """One recap card.

    Player cards are generated on first view (see the module docstring); group
    cards come from an ops run, so a missing one is a 404 — the normal answer for
    a subject below the activity floor, not an error.
    """
    if not _valid_scope(scope):
        return abort_problem(404, "Unknown recap scope")

    try:
        payload = await asyncio.to_thread(_read_snapshot, scope, subject_id, period)
    except _CorruptPayload:
        return abort_problem(500, "Recap payload could not be read")
    if payload is not None:
        return with_cache_headers(jsonify(payload), _CACHE_SECONDS)

    if scope != SCOPE_PLAYER:
        return abort_problem(404, "No recap for that period")

    conn = _redis()
    miss_key = f"recap:nocard:{scope}:{subject_id}:{period}"
    lock_key = f"recap:gen:{scope}:{subject_id}:{period}"

    if conn is not None:
        try:
            if conn.get(miss_key):
                return abort_problem(404, "No recap for that period")
        except Exception:
            conn = None

    got_lock = True
    if conn is not None:
        try:
            got_lock = bool(conn.set(lock_key, "1", nx=True, ex=_GEN_LOCK_SECONDS))
        except Exception:
            got_lock = True

    if not got_lock:
        # Someone else is already computing this exact card. Wait for their row
        # rather than duplicating a multi-second read.
        for _ in range(_LOCK_POLL_TRIES):
            await asyncio.sleep(_LOCK_POLL_SECONDS)
            try:
                payload = await asyncio.to_thread(
                    _read_snapshot, scope, subject_id, period
                )
            except _CorruptPayload:
                return abort_problem(500, "Recap payload could not be read")
            if payload is not None:
                return with_cache_headers(jsonify(payload), _CACHE_SECONDS)
        return abort_problem(404, "No recap for that period")

    # EHB is the one number on the card that comes from outside, so it is
    # fetched here — awaited, before the synchronous compute that reads it back
    # out of the cache. Bounded by its own short time budget and fail-soft: a
    # card built without it simply omits the stat, and the next visitor (or the
    # monthly run) fills it in.
    try:
        from services.recap_ehb import ensure_player_ehb

        await ensure_player_ehb(subject_id, period)
    except Exception:
        pass

    try:
        payload = await asyncio.to_thread(_generate_player, subject_id, period)
    except _CorruptPayload:
        return abort_problem(500, "Recap payload could not be read")
    except Exception:
        from db.app_logger import AppLogger

        AppLogger().log(
            log_type="error",
            data=f"recap generation failed for player {subject_id} {period}",
            app_name="web_api",
            description="get_recap",
        )
        return abort_problem(500, "Recap could not be generated")
    finally:
        if conn is not None:
            try:
                conn.delete(lock_key)
            except Exception:
                pass

    if payload is None:
        # Remember the miss: an ineligible subject would otherwise pay the full
        # compute on every visit, and a crawler could pay it for every player id.
        if conn is not None:
            try:
                conn.set(miss_key, "1", ex=_MISS_TTL_SECONDS)
            except Exception:
                pass
        return abort_problem(404, "No recap for that period")

    return with_cache_headers(jsonify(payload), _CACHE_SECONDS)


@recaps_bp.get("/recaps/<scope>/<int:subject_id>")
async def list_recaps(scope: str, subject_id: int):
    """Every period this subject has a card for, newest first.

    Backs the archive list on a profile — the "pin it to your profile forever"
    behaviour, where each past period keeps a stable URL rather than being
    replaced by the current one.
    """
    if not _valid_scope(scope):
        return abort_problem(404, "Unknown recap scope")

    with db_session() as s:
        rows = (
            s.query(RecapSnapshot.period, RecapSnapshot.generated_at)
            .filter(
                RecapSnapshot.scope == scope,
                RecapSnapshot.subject_id == subject_id,
            )
            .all()
        )

    periods = sorted(
        (
            {
                "period": p,
                # 'YYYY-MM' is 7 chars, 'YYYY' is 4 — the length is the type.
                "kind": "month" if len(p) == 7 else "year",
                "generated_at": g.isoformat() if g else None,
            }
            for p, g in rows
        ),
        key=lambda r: r["period"],
        reverse=True,
    )
    return with_cache_headers(
        jsonify({"scope": scope, "subject_id": subject_id, "periods": periods}),
        _CACHE_SECONDS,
    )
