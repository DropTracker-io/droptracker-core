"""``GET /v2/meta`` — who am I, and what may I do?

The authenticated hello-world: echoes the caller's key descriptor (id, label,
tier, effective limits, owner scope). Doubles as the end-to-end check that
auth, the tier join and limit resolution all work — and gives API consumers a
programmatic way to read their own limits instead of hardcoding them.
"""
from quart import Blueprint, g, jsonify

meta_bp = Blueprint("meta", __name__)


@meta_bp.route("/meta", methods=["GET"])
async def meta():
    descriptor = g.api_key
    return jsonify({
        "key_id": descriptor["key_id"],
        "label": descriptor["label"],
        "tier": descriptor["tier"],
        "owner_type": descriptor["owner_type"],
        "group_id": descriptor["group_id"],
        "limits": descriptor["limits"],
    })
