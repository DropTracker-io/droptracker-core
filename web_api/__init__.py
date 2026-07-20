"""DropTracker Web API v1.

A separate Quart application (recommended port :31325) that backs the first-party
Next.js front-end (droptracker-web). It reuses the existing ORM models and Redis
leaderboard data by import, but is a distinct process with its own connection
pool — the RuneLite intake API on :31323 is never touched.

Implemented surface:
  Public reads (Task 04): leaderboards, profiles, members, search.
  Auth (Task 02):    POST /auth/discord, POST /auth/logout.
  Identity (Task 02/03): GET /me, GET /me/settings, PATCH /me.
  Health/ops (Task 01):  GET /health, GET /ping, GET /metrics, GET /openapi.json.
"""
from __future__ import annotations

import json
import logging
import os
import time

from quart import Quart, jsonify, request

from web_api.common import ProblemException, problem
from web_api.routes.admin import admin_bp
from web_api.routes.announcements import announcements_bp
from web_api.routes.auth import auth_bp
from web_api.routes.badges import badges_bp
from web_api.routes.config import config_bp
from web_api.routes.docs import docs_bp
from web_api.routes.embeds import embeds_bp
from web_api.routes.event_admin import event_admin_bp
from web_api.routes.event_audit import event_audit_bp
from web_api.routes.event_board import event_board_bp
from web_api.routes.event_discord import event_discord_bp
from web_api.routes.event_participants import event_participants_bp
from web_api.routes.event_prizes import event_prizes_bp
from web_api.routes.event_templates import event_templates_bp
from web_api.routes.events import events_bp
from web_api.routes.group_admin import group_admin_bp
from web_api.routes.item_values import item_values_bp
from web_api.routes.items import items_bp
from web_api.routes.leaderboards import leaderboards_bp
from web_api.routes.lootboard import lootboard_bp
from web_api.routes.manual_submissions import manual_submissions_bp
from web_api.routes.me import me_bp
from web_api.routes.npcs import npcs_bp
from web_api.routes.paypal_ipn import paypal_ipn_bp
from web_api.routes.personal_bests import personal_bests_bp
from web_api.routes.points import points_bp
from web_api.routes.profiles import profiles_bp
from web_api.routes.realtime import realtime_bp
from web_api.routes.redirects import redirects_bp
from web_api.routes.resolve import resolve_bp
from web_api.routes.search import search_bp
from web_api.routes.submissions import submissions_bp
from web_api.routes.subscriptions import subscriptions_bp
from web_api.routes.suggestions import suggestions_bp
from web_api.routes.tickets import tickets_bp

API_PREFIX = "/api/v1"

_OPENAPI_PATH = os.path.join(os.path.dirname(__file__), "openapi.json")
_START_TIME = time.time()


def _load_openapi() -> dict:
    try:
        with open(_OPENAPI_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "openapi": "3.1.0",
            "info": {"title": "DropTracker Web API v1", "version": "1.0.0"},
            "paths": {},
        }


def create_app() -> Quart:
    app = Quart(__name__)

    logging.getLogger("quart.serving").setLevel(logging.ERROR)
    logging.getLogger("hypercorn.access").disabled = True

    # --- CORS posture: internal-only by default (§ Task 01). The BFF calls this
    # API server-to-server, so no browser CORS is needed. Enable only if an
    # explicit origin allowlist is configured. ---
    origins = [o.strip() for o in os.getenv("WEB_API_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        try:
            from quart_cors import cors

            app = cors(app, allow_origin=origins, allow_credentials=True)
        except Exception:
            logging.getLogger(__name__).warning("quart_cors unavailable; CORS not enabled")

    openapi_doc = _load_openapi()

    @app.get(f"{API_PREFIX}/health")
    async def health():
        return jsonify({"status": "ok", "service": "web-api-v1"})

    @app.get(f"{API_PREFIX}/ping")
    async def ping():
        return jsonify({"pong": True})

    @app.get(f"{API_PREFIX}/metrics")
    async def metrics():
        return jsonify(
            {
                "service": "web-api-v1",
                "uptime_seconds": int(time.time() - _START_TIME),
                "pid": os.getpid(),
            }
        )

    @app.get(f"{API_PREFIX}/openapi.json")
    async def openapi():
        return jsonify(openapi_doc)

    # --- Scoped-session hygiene (idle-transaction safety net) ---
    # Routes here use the `db_session()` context manager, but shared service /
    # util helpers (e.g. services.points) fall back to the module-level *scoped*
    # session when no session is passed. In an async app that scoped session is
    # thread-local to the event-loop thread, so a helper that issues a read
    # (autobegin) without committing/rolling back leaves an idle transaction — and
    # its connection checked out — for the life of the worker. Clearing it after
    # every request bounds any such leak to a single request. (2026-07-15
    # incident: a webapi worker held a MetaData lock on `web_events` for ~1.7h.)
    from db import session as _scoped_session

    @app.teardown_appcontext
    async def _release_scoped_session(_exc=None):
        try:
            _scoped_session.remove()
        except Exception:
            pass

    # --- Error handling ---
    @app.errorhandler(ProblemException)
    async def _handle_problem(e: ProblemException):
        return e.to_response()

    @app.errorhandler(404)
    async def _not_found(_e):
        return problem(404, "Resource not found")

    @app.errorhandler(405)
    async def _method_not_allowed(_e):
        return problem(405, "Method not allowed")

    @app.errorhandler(500)
    async def _server_error(_e):
        return problem(500, "Internal server error")

    # --- Blueprints ---
    app.register_blueprint(admin_bp, url_prefix=API_PREFIX)
    app.register_blueprint(announcements_bp, url_prefix=API_PREFIX)
    app.register_blueprint(auth_bp, url_prefix=API_PREFIX)
    app.register_blueprint(badges_bp, url_prefix=API_PREFIX)
    app.register_blueprint(config_bp, url_prefix=API_PREFIX)
    app.register_blueprint(docs_bp, url_prefix=API_PREFIX)
    app.register_blueprint(embeds_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_admin_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_audit_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_board_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_discord_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_participants_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_prizes_bp, url_prefix=API_PREFIX)
    app.register_blueprint(event_templates_bp, url_prefix=API_PREFIX)
    app.register_blueprint(events_bp, url_prefix=API_PREFIX)
    app.register_blueprint(group_admin_bp, url_prefix=API_PREFIX)
    app.register_blueprint(item_values_bp, url_prefix=API_PREFIX)
    app.register_blueprint(items_bp, url_prefix=API_PREFIX)
    app.register_blueprint(lootboard_bp, url_prefix=API_PREFIX)
    app.register_blueprint(manual_submissions_bp, url_prefix=API_PREFIX)
    app.register_blueprint(me_bp, url_prefix=API_PREFIX)
    app.register_blueprint(npcs_bp, url_prefix=API_PREFIX)
    app.register_blueprint(paypal_ipn_bp, url_prefix=API_PREFIX)
    app.register_blueprint(leaderboards_bp, url_prefix=API_PREFIX)
    app.register_blueprint(personal_bests_bp, url_prefix=API_PREFIX)
    app.register_blueprint(points_bp, url_prefix=API_PREFIX)
    app.register_blueprint(profiles_bp, url_prefix=API_PREFIX)
    app.register_blueprint(redirects_bp, url_prefix=API_PREFIX)
    app.register_blueprint(resolve_bp, url_prefix=API_PREFIX)
    app.register_blueprint(search_bp, url_prefix=API_PREFIX)
    app.register_blueprint(realtime_bp, url_prefix=API_PREFIX)
    app.register_blueprint(submissions_bp, url_prefix=API_PREFIX)
    app.register_blueprint(subscriptions_bp, url_prefix=API_PREFIX)
    app.register_blueprint(suggestions_bp, url_prefix=API_PREFIX)
    app.register_blueprint(tickets_bp, url_prefix=API_PREFIX)

    return app


__all__ = ["create_app"]
