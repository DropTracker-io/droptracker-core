"""Public Clan Log reads — a group's unique-completion board.

  GET /api/v1/groups/{id}/clan-log?period=all|YYYY|YYYY-MM
  GET /api/v1/groups/{id}/clan-log/periods

Reads are dumb on purpose, exactly like the recap: every cell was computed and
frozen by ``services/clan_log`` when the board was written, so answering is one
indexed row read. Nothing here touches ``drops`` — a live computation over a
188M-row table is what the snapshot exists to avoid.

A period the ledger can answer for but which has no stored board (an older
month nobody has opened) is built on demand and stored, the same
first-view-generates pattern ``recaps`` uses for player cards. That is cheap
here because it reads the ledger, not the drop history.

A group with no ledger at all gets an empty all-time board rather than a 404,
so long as it has members. Nothing is stored for that case — see ``_board``.

Public by construction: the board is a shareable artifact and hidden members
are already excluded when it is built.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request

from web_api.common import abort_problem, db_session, with_cache_headers

clan_log_bp = Blueprint("v1_clan_log", __name__)

# Boards move whenever a member gets something, and the refresh task runs every
# few minutes, so this is a short freshness window rather than an archive cache.
_CACHE_SECONDS = 300


def _board(group_id: int, period: str):
    """Stored board, generated from the ledger on first view if absent."""
    # Lazy import: tests stub `services` as a MagicMock at module scope.
    from services.clan_log import (
        PERIOD_ALL,
        build_payload,
        is_valid_period,
        ledger_periods,
        load_board,
        save_board,
        visible_group_player_ids,
    )

    if not is_valid_period(period):
        return None

    with db_session() as s:
        payload = load_board(s, group_id, period)
        if payload is not None:
            return payload

        # A period the ledger can answer for is built and stored on first view.
        if period in set(ledger_periods(s, group_id)):
            payload = build_payload(s, group_id, period)
            save_board(s, group_id, period, payload)
            s.commit()
            return payload

        # No ledger rows at all — the group has never been swept, or has been
        # swept and genuinely has nothing yet. Either way a clan that exists and
        # has members gets its (empty) all-time board rather than a 404: an
        # honest 0/N is what a freshly configured clan should see, and the 404
        # was what every group created after the feature shipped got, because
        # the sweep used to take its candidates from the ledger it populates.
        #
        # Deliberately NOT stored. The sweep owns the stored board, and this one
        # stops being true the moment that group's first sweep lands, so caching
        # it here would pin a real-looking 0% in place. Narrower periods stay a
        # 404: `is_valid_period` accepts any well-formed YYYY-MM, and building
        # those on demand would mint snapshots for arbitrary crafted URLs.
        if period != PERIOD_ALL or not visible_group_player_ids(s, group_id):
            return None
        return build_payload(s, group_id, period)


def _periods(group_id: int) -> list[str]:
    from services.clan_log import ledger_periods

    with db_session() as s:
        return ledger_periods(s, group_id)


@clan_log_bp.get("/groups/<int:group_id>/clan-log")
async def get_clan_log(group_id: int):
    """One group's board for one period."""
    from services.clan_log import PERIOD_ALL, is_valid_period

    period = (request.args.get("period") or PERIOD_ALL).strip()
    if not is_valid_period(period):
        abort_problem(400, "Invalid period", "Use 'all', 'YYYY' or 'YYYY-MM'.")

    payload = await asyncio.to_thread(_board, group_id, period)
    if payload is None:
        abort_problem(
            404,
            "No clan log",
            "This group has no Clan Log for that period yet.",
        )
    return with_cache_headers(jsonify(payload), max_age=_CACHE_SECONDS)


@clan_log_bp.get("/groups/<int:group_id>/clan-log/periods")
async def get_clan_log_periods(group_id: int):
    """Every period this group's board can be shown for — the page's picker."""
    periods = await asyncio.to_thread(_periods, group_id)
    return with_cache_headers(
        jsonify({"periods": periods}), max_age=_CACHE_SECONDS
    )
