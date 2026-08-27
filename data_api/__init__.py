"""DropTracker external data API (v2) — the third HTTP service.

Serves authenticated player/group data at ``api.droptracker.io/v2/`` from its
own process (port 31326, ``droptracker-data-api.service``) so that expensive
reads can never block submission intake (:31323) or the site backend (:31325).
That isolation is the reason this package exists; do not fold these routes
into either existing app.

Ground rules for every route added here (dev-tracker #15):
    * Auth is mandatory. Everything except /v2/health goes through the bearer
      key check in ``data_api.auth``; there are no anonymous endpoints.
    * All ORM work runs inside ``asyncio.to_thread`` — sync DB calls on the
      event loop are how the export API stalls its worker; never copy that.
    * Sessions come from ``data_api.core`` (dedicated engine, 10s server-side
      statement ceiling). A timeout surfaces as a clean 503, never a 500.
    * Privacy parity with the site: a hidden player/user 404s identically to
      a missing one, and per-account visibility settings are honored. The
      external API never reveals more than droptracker.io would.
"""
import os

from quart import Quart, g, jsonify, request

API_PREFIX = "/v2"

#: Paths under /v2 that skip authentication (exact match).
_PUBLIC_PATHS = frozenset({f"{API_PREFIX}/health"})


def create_app() -> Quart:
    app = Quart(__name__)
    app.config["JSON_SORT_KEYS"] = False

    from data_api.routes.health import health_bp
    from data_api.routes.meta import meta_bp

    app.register_blueprint(health_bp, url_prefix=API_PREFIX)
    app.register_blueprint(meta_bp, url_prefix=API_PREFIX)

    @app.before_request
    async def _authenticate():
        if request.path in _PUBLIC_PATHS:
            return None
        from data_api.auth import authenticate_request

        error_response = await authenticate_request()
        return error_response  # None lets the request through

    @app.errorhandler(404)
    async def _not_found(_error):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(405)
    async def _bad_method(_error):
        return jsonify({"error": "method_not_allowed"}), 405

    @app.errorhandler(500)
    async def _server_error(_error):
        return jsonify({"error": "internal_error"}), 500

    return app
