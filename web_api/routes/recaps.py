"""Public recap ("Wrapped") reads.

  GET /api/v1/recaps/{scope}/{subject_id}/{period}   one card
  GET /api/v1/recaps/{scope}/{subject_id}            the archive index

Read-only, and deliberately dumb: every number was computed and frozen by
``services/recap.py`` when the snapshot was written. Nothing is recomputed at
request time, which is what lets a recap URL stay valid forever and answer in a
single indexed row read.

A recap is public by construction — it's a shareable artifact, and gating it
would defeat the point. The only privacy control that applies is the one already
baked into the payload: hidden players were excluded when it was generated.
"""
from __future__ import annotations

import json

from quart import Blueprint, jsonify

from db import RecapSnapshot
from db.models.recap import RECAP_SCOPES
from web_api.common import abort_problem, db_session, with_cache_headers

recaps_bp = Blueprint("v1_recaps", __name__)

# A settled period's card never changes, so it can be cached hard. The one
# exception is a period regenerated after a backfill (e.g. the NPC rollup gap),
# which is rare and tolerable — an hour of staleness on an archive page.
_CACHE_SECONDS = 3600


def _valid_scope(scope: str) -> bool:
    return scope in RECAP_SCOPES


@recaps_bp.get("/recaps/<scope>/<int:subject_id>/<period>")
async def get_recap(scope: str, subject_id: int, period: str):
    """One recap card. 404 when it hasn't been generated — which is the normal
    answer for a subject below the activity floor, not an error."""
    if not _valid_scope(scope):
        return abort_problem(404, "Unknown recap scope")

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
        if not row:
            return abort_problem(404, "No recap for that period")
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            # A corrupt row is a server problem, not a missing page.
            return abort_problem(500, "Recap payload could not be read")
        payload["schema_version"] = int(row.schema_version or 1)
        payload["generated_at"] = (
            row.generated_at.isoformat() if row.generated_at else None
        )

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
