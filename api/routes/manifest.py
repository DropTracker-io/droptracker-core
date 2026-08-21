"""``GET /manifest`` — server-controlled reference data for the RuneLite plugin.

Every plugin client fetches this once per session, so the handler is built to
never be the reason a client fails to start: a database problem serves the
built-in defaults rather than an error, and the response is cached in Redis so a
crowd of clients starting at once costs one query.

Why an endpoint rather than the GitHub Pages content channel we already publish
``valued_items.txt`` through: the point of the manifest is that a wrong value is
fixable in minutes, and Pages adds a build + CDN cache we do not control. If the
payload later grows large (the collection log structure is the candidate), the
bulky sections can move to Pages without the plugin caring — it only knows a URL.
"""
import asyncio
import json

from quart import Blueprint, Response, jsonify, request

from api.core import get_db_session, redis_client
# Import the model from the package, not its submodule: the unit-test conftest
# stubs ``db.models`` in sys.modules, so a submodule import ("db.models.x")
# fails at collection for every test that imports ``api``. Same reason route
# modules import services lazily.
from db.models import PluginManifestSection
from services.plugin_manifest import CACHE_KEY, CACHE_TTL_SECONDS, manifest_payload

manifest_bp = Blueprint("manifest", __name__)

# Long enough that a restart storm costs one query, short enough that a fix
# lands without anyone waiting. Clients hold their copy for a whole session
# regardless, so this only bounds propagation to *new* sessions — and
# scripts/build_manifest.py busts the key outright when it writes.
_CACHE_KEY = CACHE_KEY
_CACHE_TTL_SECONDS = CACHE_TTL_SECONDS


def _load_manifest() -> dict:
    """Assemble the manifest from the database.

    Called in a worker thread — the intake API is async and this is blocking I/O.
    """
    db_session = get_db_session()
    try:
        rows = db_session.query(PluginManifestSection).all()
    finally:
        db_session.close()
    return manifest_payload(rows)


@manifest_bp.get("/manifest")
async def get_manifest():
    try:
        cached = await asyncio.to_thread(redis_client.get, _CACHE_KEY)
    except Exception as exc:
        print(f"/manifest cache read failed: {exc}")
        cached = None

    if cached:
        try:
            return _respond(json.loads(cached))
        except ValueError:
            # Poisoned cache entry: fall through and rebuild rather than 500.
            print("/manifest cached value was not valid JSON; rebuilding")

    try:
        payload = await asyncio.to_thread(_load_manifest)
    except Exception as exc:
        # Defaults keep clients reading the right varps; refusing to answer
        # would leave them with no varp list at all.
        print(f"/manifest assembly failed, serving defaults: {exc}")
        return _respond(manifest_payload([]))

    try:
        await asyncio.to_thread(
            redis_client.setex, _CACHE_KEY, _CACHE_TTL_SECONDS, json.dumps(payload)
        )
    except Exception as exc:
        print(f"/manifest cache write failed: {exc}")

    return _respond(payload)


def _respond(payload: dict):
    """Serve the manifest, honouring If-None-Match.

    The ETag is the manifest version, itself a hash of the section payloads, so
    an unchanged manifest answers 304 and the client keeps what it has.
    """
    etag = f'"{payload.get("version", "unknown")}"'
    if request.headers.get("If-None-Match") == etag:
        response = Response("", status=304)
    else:
        response = jsonify(payload)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"public, max-age={_CACHE_TTL_SECONDS}"
    return response
