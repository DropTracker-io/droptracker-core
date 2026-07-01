"""DropTracker Web API v1.

A separate Quart application (recommended port :31325) that backs the first-party
Next.js front-end (droptracker-web). It reuses the existing ORM models and Redis
leaderboard data by import, but is a distinct process with its own connection
pool — the RuneLite intake API on :31323 is never touched.

Currently implements the Phase-1 public read surface (Task 04):
  GET /api/v1/health
  GET /api/v1/leaderboards/players
  GET /api/v1/leaderboards/groups
  GET /api/v1/players/{id}
  GET /api/v1/players/search
  GET /api/v1/groups/{id}
  GET /api/v1/groups/{id}/members
  GET /api/v1/groups/search
  GET /api/v1/search
"""
from __future__ import annotations

import logging

from quart import Quart, jsonify

from web_api.common import problem
from web_api.routes.leaderboards import leaderboards_bp
from web_api.routes.profiles import profiles_bp
from web_api.routes.search import search_bp

API_PREFIX = "/api/v1"


def create_app() -> Quart:
    app = Quart(__name__)

    logging.getLogger("quart.serving").setLevel(logging.ERROR)
    logging.getLogger("hypercorn.access").disabled = True

    @app.get(f"{API_PREFIX}/health")
    async def health():
        return jsonify({"status": "ok", "service": "web-api-v1"})

    @app.errorhandler(404)
    async def _not_found(_e):
        return problem(404, "Resource not found")

    @app.errorhandler(500)
    async def _server_error(_e):
        return problem(500, "Internal server error")

    app.register_blueprint(leaderboards_bp, url_prefix=API_PREFIX)
    app.register_blueprint(profiles_bp, url_prefix=API_PREFIX)
    app.register_blueprint(search_bp, url_prefix=API_PREFIX)

    return app


__all__ = ["create_app"]
