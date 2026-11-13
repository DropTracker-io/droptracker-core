"""Capture real Data API (v2) responses for the public reference page.

The docs at /docs/api show what each endpoint and section actually returns.
Those examples are captured here from the running code against live data, so
they cannot drift from the API the way hand-written samples do.

    ./venv/bin/python -m scripts.capture_api_examples                 # print a summary
    ./venv/bin/python -m scripts.capture_api_examples --write PATH    # write the JSON

The web repo keeps the result at ``apps/web/lib/api-examples.json``; re-run
with ``--write`` after changing a loader's output and commit the file there.

No key is minted and nothing is metered: the data API app runs in-process
with a synthetic key descriptor and the rate-limit/usage hooks switched off,
so the capture reads the database and nothing else. Long arrays and maps are
cut down (the cut is marked in the text), and Discord ids are replaced with a
placeholder. The subjects are the owner's own account and group.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone

#: Whose data appears on a public page: the owner's account and group.
PLAYER = "joelhalen"
GROUP_ID = 31
#: The owner's group runs no public events; this one (a staff member's) does.
EVENTS_GROUP_ID = 7
BASE = "https://api.droptracker.io"

_MORE = "__dt_more__"
_LIMITS = {"requests_per_min": 60, "cost_units_per_min": 200000,
           "requests_per_day": 20000, "max_concurrency": 4}


def _descriptor(scope: str, group_id: int = GROUP_ID) -> dict:
    return {
        "key_id": 12, "label": "Clan website", "tier": "standard",
        "scope": scope, "owner_type": scope, "owner_user_id": None,
        "group_id": group_id if scope == "group" else None,
        "limits": dict(_LIMITS), "can_submit": False, "submit_preapproved": False,
    }


def trim(value, *, items: int = 3, keys: int = 12, depth: int = 0, path: str = ""):
    """Cut long lists/maps down to a readable sample, marking each cut."""
    if isinstance(value, list):
        keep = items if depth else max(items, 2)
        if value and all(isinstance(v, (int, float, str)) for v in value):
            keep = 8  # id arrays read fine on one line
        out = [trim(v, items=items, keys=keys, depth=depth + 1, path=path) for v in value[:keep]]
        if len(value) > keep:
            out.append(f"{_MORE}{len(value) - keep} more")
        return out
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= keys:
                out[_MORE] = f"{len(value) - keys} more"
                break
            if k == "discord_id" and v:
                v = "123456789012345678"
            out[k] = trim(v, items=items, keys=keys, depth=depth + 1, path=f"{path}.{k}")
        return out
    if isinstance(value, str) and len(value) > 160:
        return value[:157] + "..."
    return value


def render(value) -> str:
    """Pretty JSON with each cut written as a ``// … N more`` comment."""
    text = json.dumps(value, indent=2, ensure_ascii=False)
    text = re.sub(rf',?\n(\s*)"{_MORE}": "(\d+) more"', r",\n\1// … \2 more", text)
    text = re.sub(rf',?\n(\s*)"{_MORE}(\d+) more"', r",\n\1// … \2 more", text)
    return text


def _pick(payload: dict, *path):
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def plan(sections: list[str]) -> list[dict]:
    """Every request to capture. ``section`` rows keep only that section."""
    g, p = GROUP_ID, PLAYER
    out = [
        dict(id="health", scope="group", path="/v2/health"),
        dict(id="meta", scope="group", path="/v2/meta"),
        dict(id="sections", scope="group", path="/v2/sections"),
        dict(id="player", scope="group", path=f"/v2/players/{p}?include=identity,stats,loot",
             keys=30),
        dict(id="roster", scope="group",
             path=f"/v2/groups/{g}/players?include=identity,loot,meta&limit=2"),
        dict(id="group_events", scope="group", group=EVENTS_GROUP_ID,
             path=f"/v2/groups/{EVENTS_GROUP_ID}/events?past_limit=1", keys=24),
        dict(id="groups", scope="global", path="/v2/groups?limit=2&include=meta"),
        dict(id="players", scope="global", path="/v2/players?include=identity,loot&limit=2"),
        dict(id="npcs", scope="group", path="/v2/npcs?q=doom"),
        dict(id="npc", scope="group", path="/v2/npcs/2042"),
        dict(id="collection_log", scope="group", path="/v2/collection-log", keys=6, items=2),
        dict(id="error_scope", scope="group", path="/v2/groups/7/players"),
        dict(id="error_section", scope="group", path=f"/v2/players/{p}?include=loot,banana"),
    ]
    for key in sections:
        if key in ("identity", "drop_ids"):
            continue
        include = "drops,drop_ids&max_drops=2&since={since}" if key == "drops" else key
        out.append(dict(id=f"section:{key}", scope="group", section=key,
                        path=f"/v2/players/{p}?include={include}"))
    return out


async def capture() -> dict:
    import data_api
    import data_api.auth as auth
    import data_api.serving as serving
    from data_api import sections as sect
    from data_api.limits import LimitDecision
    from quart import g

    current = {"scope": "group", "group": GROUP_ID}

    async def _authenticate():
        g.api_key = _descriptor(current["scope"], current["group"])
        return None

    class _Slot:
        acquired = True

        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}
    auth.authenticate_request = _authenticate
    serving.check_and_charge = lambda *_a, **_k: LimitDecision(True, dict(headers))
    serving.Concurrency = _Slot
    serving.usage.record = lambda *_a, **_k: None
    serving.usage.touch_last_used = lambda *_a, **_k: False

    app = data_api.create_app()
    client = app.test_client()
    registry = getattr(sect, "REGISTRY", None) or getattr(sect, "SECTIONS", {})
    since = int(datetime.now(timezone.utc).timestamp()) - 7 * 86400
    examples = []
    for item in plan(list(registry)):
        current["scope"] = item["scope"]
        current["group"] = item.get("group", GROUP_ID)
        path = item["path"].format(since=since)
        response = await client.get(path)
        body = await response.get_json()
        shown = body
        if item.get("section") and response.status_code == 200:
            # One section, without the identity block every response carries.
            shown = {item["section"]: _pick(body, "player", item["section"])}
        text = render(trim(shown, items=item.get("items", 3), keys=item.get("keys", 12)))
        examples.append({
            "id": item["id"],
            "request": f"GET {BASE}{path}",
            "status": response.status_code,
            "body": text,
        })
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "player": PLAYER,
        "group_id": GROUP_ID,
        "examples": examples,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", metavar="PATH", help="write the JSON here")
    ap.add_argument("--show", metavar="ID", help="print one captured body")
    args = ap.parse_args()
    data = asyncio.run(capture())
    for ex in data["examples"]:
        print(f"{ex['status']}  {len(ex['body']):>6}  {ex['id']:<28} {ex['request']}")
        if args.show and (args.show == ex["id"] or args.show == "all"):
            print(ex["body"])
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {len(data['examples'])} examples to {args.write}")
    else:
        print("dry run: pass --write PATH to save")
    return 0


if __name__ == "__main__":
    sys.exit(main())
