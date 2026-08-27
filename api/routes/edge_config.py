"""``GET /edge-config`` — runtime configuration for the Cloudflare edge Worker.

The Worker in edge/intake-capture sits on POST /webhook and, until now, took
every one of its settings from wrangler.toml — meaning any behaviour change
needed a redeploy. This endpoint is the one thing it polls, so the admin panel
can flip the dev mirror on and off without touching Cloudflare.

It lives on the intake API rather than web_api for a plain reason: web_api is
internal-only and nginx does not expose it, while the Worker has to reach this
over the public internet as ``api-origin.droptracker.io/edge-config``. That host
carries no Worker route, so fetching it cannot re-enter the Worker.

Unauthenticated on purpose. The document holds no secret and names no host (see
services.edge_config.edge_payload), so a token would buy nothing but another
credential to rotate. Cache-Control lets Cloudflare absorb the polling: the real
query rate is a handful per minute regardless of how many colos are asking.
"""
import asyncio

from quart import Blueprint, Response, jsonify, request
from werkzeug.http import parse_etags

edge_config_bp = Blueprint("edge_config", __name__)

#: Matches the Worker's own config TTL. Propagation after a toggle is this plus
#: the Worker's memo, so ~60s worst case — which is what the admin panel says.
_CACHE_TTL_SECONDS = 30


@edge_config_bp.get("/edge-config")
async def get_edge_config():
    # Lazy import: tests/conftest.py stubs `services` in sys.modules, so a
    # module-level import breaks collection for everything that imports `api`.
    from services.edge_config import DISABLED, edge_payload, mirror_config

    try:
        mirror = await asyncio.to_thread(mirror_config)
    except Exception as exc:
        # mirror_config() already swallows its own errors; this is belt and
        # braces for a thread-pool failure. Serving "off" is the safe answer —
        # the Worker treats an unreadable config as a reason to stop mirroring.
        print(f"/edge-config read failed, serving disabled: {exc}")
        mirror = dict(DISABLED)

    return _respond(edge_payload(mirror))


def _etag_matches(if_none_match: str, version: str) -> bool:
    """Whether the client already holds this version.

    Weak comparison, for the same reason as api/routes/manifest.py: nginx
    rewrites a strong ETag to its weak form when it gzips, so a strict string
    compare never matches for any client that accepts gzip.
    """
    return parse_etags(if_none_match).contains_weak(version)


def _respond(payload: dict):
    version = str(payload.get("version", "unknown"))
    if _etag_matches(request.headers.get("If-None-Match"), version):
        response = Response("", status=304)
    else:
        response = jsonify(payload)
    response.headers["ETag"] = f'"{version}"'
    response.headers["Cache-Control"] = f"public, max-age={_CACHE_TTL_SECONDS}"
    return response
