"""Unauthenticated liveness probe (the only public path on this service)."""
import os
import time

from quart import Blueprint, jsonify

health_bp = Blueprint("health", __name__)

_STARTED_AT = time.time()


@health_bp.route("/health", methods=["GET"])
async def health():
    return jsonify({
        "service": "data_api",
        "status": "ok",
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "pid": os.getpid(),
    })
