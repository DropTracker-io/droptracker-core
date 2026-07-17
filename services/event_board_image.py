"""Render a live event board (bingo or board game) to a PNG for Discord.

The website already renders rich, interactive boards — the bingo grid
(``components/bingo-board.tsx``) and the board-game dice-track
(``components/event-board-view.tsx``). Rather than re-draw them, we screenshot
the REAL board so the Discord image is pixel-for-pixel what players see: the
chrome-less page ``/board-image/{id}`` mounts the same components, and
:mod:`services.page_screenshot` captures it with headless chromium over CDP.

Layers:
  * :func:`_collect_render_inputs` — the DB read, reduced to a **state
    signature**: the visual-board kind + a hash of everything that changes the
    picture (completions / positions / teams / tasks). Drives cache invalidation.
  * :func:`render_event_board_png` — screenshot the export page → PNG bytes
    (powers ``GET .../board.png`` and the cached edge below).
  * :func:`board_image_with_hash` / :func:`board_image_png` — the cached edge
    the hot callers use: screenshot once, cache the PNG bytes in Redis keyed by
    a state-hash, and skip re-shooting an unchanged board. ``team_id`` renders
    the board with that team's tab selected (web54a team-channel posts) under
    its own cache keys. Callers attach the bytes as a Discord file
    (``attachment://…``) — V2 media galleries render attachments reliably where
    external URLs spin forever. Fully fail-open — any error returns ``None``
    and the caller just omits the image.

Config (env): ``BOARD_IMAGE_TOKEN`` (shared secret gating the export route;
unset ⇒ feature disabled), ``BOARD_IMAGE_BASE_URL`` (the origin chromium hits;
default the public site).
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional
from urllib.parse import quote

from db.app_logger import AppLogger

app_logger = AppLogger()

# The origin chromium loads the export page from. Public site by default
# (colour-agnostic vs. the blue/green Next pair; egress is already open); can be
# pointed at http://127.0.0.1:3138x later to skip Cloudflare.
BOARD_IMAGE_BASE_URL = os.getenv(
    "BOARD_IMAGE_BASE_URL", "https://www.droptracker.io").rstrip("/")

# CSS layout width of the export page — keep in sync with WIDTH in
# apps/web/app/board-image/[id]/page.tsx.
BOARD_IMAGE_WIDTH = 1100
# Device pixel ratio for a crisp capture (2 = retina).
BOARD_IMAGE_SCALE = 2.0

_CACHE_TTL_SECONDS = 6 * 3600


def _board_image_token() -> str:
    return os.environ.get("BOARD_IMAGE_TOKEN", "")


def board_image_page_url(event_id: int, team_id=None) -> str:
    """The chrome-less export page URL the bot screenshots. ``team_id``
    renders the board with that team's tab selected (web54a team-channel
    posts) instead of the all-teams view."""
    url = (f"{BOARD_IMAGE_BASE_URL}/board-image/{int(event_id)}"
           f"?k={quote(_board_image_token(), safe='')}")
    if team_id is not None:
        url += f"&team={int(team_id)}"
    return url


# --------------------------------------------------------------------------- #
# DB read → state signature (kind + change hash)
# --------------------------------------------------------------------------- #
def _bingo_signature(session, event, team_id=None) -> Optional[dict]:
    from db.models import (
        EventBingoCell, EventBingoCompletion, EventTask, EventTeam,
    )

    cells = (session.query(EventBingoCell)
             .filter(EventBingoCell.event_id == event.id)
             .order_by(EventBingoCell.idx.asc()).all())
    if not cells:
        return None
    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id == event.id)
             .order_by(EventTeam.id.asc()).all())
    tasks = (session.query(EventTask)
             .filter(EventTask.event_id == event.id).all())

    cell_ids = [c.id for c in cells]
    comps = (session.query(EventBingoCompletion)
             .filter(EventBingoCompletion.cell_id.in_(cell_ids)).all()
             if cell_ids else [])
    teams_by_cell: dict[int, list[int]] = {}
    for comp in comps:
        if comp.team_id is None:
            continue
        # Team-scoped render (web54a): with the team's tab selected only that
        # team's marks are visible, so foreign teams' completions must not
        # bust its cache / trigger an edit.
        if team_id is not None and comp.team_id != team_id:
            continue
        teams_by_cell.setdefault(comp.cell_id, [])
        if comp.team_id not in teams_by_cell[comp.cell_id]:
            teams_by_cell[comp.cell_id].append(comp.team_id)

    return {
        "kind": "bingo",
        "name": event.name,
        "status": event.status,
        "team": team_id,
        "cells": [(c.idx, c.label, c.task_id, sorted(teams_by_cell.get(c.id, [])))
                  for c in cells],
        "tasks": sorted((t.id, t.label, t.type, t.target, t.target_value)
                        for t in tasks),
        "teams": [(t.id, t.name, t.color) for t in teams],
        # Pending-review overlay (web53a): amber tiles change the picture too,
        # so pending (task, team) pairs must bust the cache.
        "pending": _pending_pairs(session, event, team_id),
    }


def _pending_pairs(session, event, team_id=None) -> list:
    from db.models import EventCompletion

    query = (session.query(EventCompletion.task_id, EventCompletion.team_id)
             .filter(EventCompletion.event_id == event.id,
                     EventCompletion.status == "pending"))
    if team_id is not None:
        query = query.filter(EventCompletion.team_id == team_id)
    return sorted(
        (tid, tteam) for tid, tteam in query.distinct().all()
        if tteam is not None
    )


def _board_game_signature(session, event) -> Optional[dict]:
    from db.models import (
        EventBoardConfig, EventBoardPosition, EventBoardTile, EventTeam,
    )

    config = (session.query(EventBoardConfig)
              .filter(EventBoardConfig.event_id == event.id).first())
    tiles = (session.query(EventBoardTile)
             .filter(EventBoardTile.event_id == event.id)
             .order_by(EventBoardTile.idx.asc()).all())
    if not tiles or not config or not config.background_url:
        return None

    teams = {t.id: t for t in session.query(EventTeam)
             .filter(EventTeam.event_id == event.id).all()}
    positions = (session.query(EventBoardPosition)
                 .filter(EventBoardPosition.event_id == event.id).all())

    return {
        "kind": "board_game",
        "status": event.status,
        "bg": config.background_url,
        "wh": (config.bg_width, config.bg_height),
        "settings": config.settings,
        "tiles": [(t.idx, t.difficulty, t.tile_kind, round(t.x or 0.0, 4),
                   round(t.y or 0.0, 4)) for t in tiles],
        # Coarse per-team state — tile / status / turn / task / coins / piece.
        # (Raw task progress is deliberately excluded so the image doesn't
        # re-render on every increment; it refreshes on real moves.)
        "positions": sorted(
            (p.team_id, int(p.tile_idx or 0), p.status, int(p.turns_completed or 0),
             p.current_task_id,
             int(getattr(teams.get(p.team_id), "coins", 0) or 0),
             getattr(teams.get(p.team_id), "piece_item_id", None),
             getattr(teams.get(p.team_id), "color", None))
            for p in positions),
    }


def _collect_render_inputs(session, event, team_id=None) -> Optional[dict]:
    """State signature for one event's board view: ``{"kind", "hash_src"}`` —
    or ``None`` for events with no visual board (standard task-list events).

    ``hash_src`` folds everything that changes the rendered picture, so the
    Redis cache re-screenshots exactly when the board actually changes.
    ``team_id`` scopes a bingo signature to that team's tab-selected view; a
    board game's shared track keeps a team-agnostic signature (the team param
    only highlights "their" piece — the per-team cache keys keep the
    highlighted variants apart)."""
    if getattr(event, "has_bingo", False):
        sig = _bingo_signature(session, event, team_id)
    elif getattr(event, "kind", None) == "board_game":
        sig = _board_game_signature(session, event)
    else:
        return None
    if sig is None:
        return None
    return {"kind": sig["kind"], "hash_src": sig}


def _state_hash(inputs: dict) -> str:
    return hashlib.sha1(
        json.dumps(inputs.get("hash_src"), sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Render (screenshot) + cached edge
# --------------------------------------------------------------------------- #
async def screenshot_event_board(event_id: int, *, scale: float = BOARD_IMAGE_SCALE,
                            team_id=None) -> Optional[bytes]:
    """Screenshot the export page for one event → PNG bytes, or ``None`` when the
    feature is unconfigured / on any failure (fail-open)."""
    if not _board_image_token():
        app_logger.log(
            log_type="warning",
            data="BOARD_IMAGE_TOKEN is unset — event board images are disabled.",
            app_name="event_board_image", description="screenshot_event_board")
        return None
    from services.page_screenshot import screenshot_url

    try:
        return await screenshot_url(
            board_image_page_url(event_id, team_id=team_id),
            width=BOARD_IMAGE_WIDTH, scale=scale)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Board screenshot failed for event {event_id}: {e}",
            app_name="event_board_image", description="screenshot_event_board")
        return None


async def render_event_board_png(session, event, *, scale: float = BOARD_IMAGE_SCALE
                                 ) -> Optional[bytes]:
    """Full board → PNG bytes, or ``None`` when the event has no visual board /
    on any failure. Powers ``GET /events/{id}/board.png``."""
    inputs = _collect_render_inputs(session, event)
    if not inputs:
        return None
    return await screenshot_event_board(event.id, scale=scale)


async def board_image_with_hash(session, event, team_id=None):
    """``(png_bytes, state_hash, rendered)`` for one board view — the whole
    event, or one team's tab-selected view (web54a team-channel posts).

    ``state_hash`` lets callers keep their own last-rendered marker
    (``web_event_team_discord.board_state_hash``) and skip Discord edits when
    nothing changed; ``rendered`` is True only when a real screenshot ran (the
    callers' per-tick render budgets count those, not cache hits).
    ``(None, None, False)`` for non-visual events or on any failure."""
    try:
        inputs = _collect_render_inputs(session, event, team_id)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Board image inputs failed for event "
                 f"{getattr(event, 'id', '?')}: {e}",
            app_name="event_board_image", description="board_image_with_hash")
        return None, None, False
    if not inputs:
        return None, None, False

    event_id = event.id
    state_hash = _state_hash(inputs)
    suffix = f":t{int(team_id)}" if team_id is not None else ""
    hash_key = f"event:{event_id}:board_img{suffix}:hash"
    png_key = f"event:{event_id}:board_img{suffix}:png"

    try:
        # Raw client: the wrapper's get() utf-8-decodes, which corrupts PNGs.
        from utils.redis import redis_client

        cached_hash = redis_client.get(hash_key)
        if cached_hash == state_hash:
            cached_png = redis_client.client.get(png_key)
            if cached_png:
                return bytes(cached_png), state_hash, False
    except Exception:
        pass  # cache read is best-effort; press on and render

    png = await screenshot_event_board(event_id, team_id=team_id)
    if not png:
        return None, None, False

    try:
        from utils.redis import redis_client

        redis_client.client.setex(png_key, _CACHE_TTL_SECONDS, png)
        redis_client.setex(hash_key, _CACHE_TTL_SECONDS, state_hash)
    except Exception:
        pass  # a cache-write miss just means we re-render next time
    return png, state_hash, True


async def board_image_png(session, event) -> Optional[bytes]:
    """PNG bytes of the current (all-teams) board — see
    :func:`board_image_with_hash`, which this delegates to.

    Callers attach the bytes as a Discord **file** and reference it as
    ``attachment://…`` — Discord's Components-V2 media galleries render
    attachments reliably where external URLs spin forever. ``None`` for
    non-visual events or on any failure — every caller treats a missing image
    as "just send the text"."""
    png, _state_hash, _rendered = await board_image_with_hash(session, event)
    return png
