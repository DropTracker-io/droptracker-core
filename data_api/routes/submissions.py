"""``/v2/submissions`` — the submission API (suggestion #198).

Lets a clan's own tooling (typically its Discord bot, after staff accept a
screenshot) submit drops, collection log slots, personal bests, combat
achievements and pets for its members, through exactly the pipeline the
website's /submit form and the Discord ``/submit`` command use.

The data API's DB user is SELECT-only, so this module only *reads*: it
authorizes the key, checks the player belongs to the key's group, validates
the screenshot, and forwards everything to the intake's ``/manual-submit``
(shared-secret, server-to-server), which does every write — see
``data/submissions/api_submissions.py``.

Who may call it: a **group** key with ``can_submit`` (staff-granted at
/admin/api-keys). With ``submit_preapproved`` too, the group's own review is
taken as done: no manual-submission review for that group, and plugin-level
trust on events it hosts or takes part in.
"""
from __future__ import annotations

import asyncio
import io
import os
import time
from urllib.parse import urlparse

from quart import Blueprint, g, jsonify, request

from data_api import usage
from data_api.core import SessionLocal, is_statement_timeout
from data_api.limits import Concurrency, check_and_charge

submissions_bp = Blueprint("submissions", __name__)

INTAKE_API_URL = os.getenv("INTAKE_API_URL", "http://127.0.0.1:31323")
MANUAL_SUBMIT_KEY_HEADER = "X-DT-Manual-Key"
INTAKE_TIMEOUT_SECONDS = 45.0

#: Flat cost per call. A submission is one player, but it writes, so it is
#: priced like a heavy read rather than a free one.
SUBMISSION_COST = 50
#: Hard per-key ceiling on submissions per minute, on top of the tier's
#: request budget: a tier sized for reads must not become a write firehose.
SUBMISSIONS_PER_MIN = int(os.getenv("DATA_API_SUBMISSIONS_PER_MIN", "30"))

#: Public type name -> intake submission_type.
TYPE_MAP = {
    "drop": "drop",
    "clog": "collection_log",
    "pb": "personal_best",
    "ca": "combat_achievement",
    "pet": "pet",
}
CA_TIERS = ("easy", "medium", "hard", "elite", "master", "grandmaster")

IMAGE_MAX_BYTES = 10 * 1024 * 1024
#: Pillow format -> (extension, content type). Same set as the site's
#: proof uploads (web_api/routes/submissions.py).
IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}
#: The only hosts an ``image_url`` is fetched from. Discord attachment links
#: are what a clan bot has in hand (and they expire, so we copy the bytes
#: rather than storing the link). Anything else must be uploaded as a file:
#: fetching arbitrary URLs from inside our network is how SSRF happens.
IMAGE_URL_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})

MAX_TEXT = 120


class SubmissionError(Exception):
    """A caller mistake: ``(status, error, detail)`` for the response."""

    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


def _bad(detail: str, error: str = "invalid_submission") -> SubmissionError:
    return SubmissionError(422, error, detail)


def _json(payload, status: int, headers: dict | None = None):
    response = jsonify(payload)
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


# ── body parsing (pure; unit-tested) ─────────────────────────────────────────

def _text(body: dict, name: str, required: bool = False, max_len: int = MAX_TEXT):
    value = body.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _bad(f"'{name}' is required.")
        return None
    if isinstance(value, (dict, list, bool)):
        raise _bad(f"'{name}' must be text.")
    value = str(value).strip()
    if len(value) > max_len:
        raise _bad(f"'{name}' must be at most {max_len} characters.")
    return value


def _int(body: dict, name: str, minimum: int = 0, maximum: int = 2 ** 31 - 1,
         required: bool = False):
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise _bad(f"'{name}' is required.")
        return None
    if isinstance(value, bool):
        raise _bad(f"'{name}' must be a whole number.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise _bad(f"'{name}' must be a whole number.")
    if number < minimum or number > maximum:
        raise _bad(f"'{name}' must be between {minimum} and {maximum}.")
    return number


def _bool(body: dict, name: str, default: bool) -> bool:
    value = body.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise _bad(f"'{name}' must be true or false.")


def parse_time_ms(value) -> int | None:
    """A kill time as milliseconds: an int is already ms; ``"1:23.4"``,
    ``"1:02:03"`` or ``"83.4"`` are clock text."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _bad("'time' must be a kill time like 1:23.4.")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) == 3 else 0
        if seconds < 0 or minutes < 0 or hours < 0:
            raise ValueError
    except ValueError:
        raise _bad("'time' must be a kill time like 1:23.4.")
    return int(round(((hours * 60 + minutes) * 60 + seconds) * 1000))


def build_intake_payload(body: dict) -> tuple[str, dict]:
    """``(public_type, intake_payload)`` from a request body, or raise
    :class:`SubmissionError`. The player and the api_* fields are added by
    the caller once the key and player are resolved."""
    sub_type = (_text(body, "type", required=True) or "").lower()
    if sub_type not in TYPE_MAP:
        raise _bad(f"'type' must be one of {', '.join(TYPE_MAP)}.")
    payload: dict = {"submission_type": TYPE_MAP[sub_type], "world_type": "main"}

    item = _text(body, "item")
    npc = _text(body, "npc")
    if sub_type == "drop":
        if not item or not npc:
            raise _bad("A drop needs both 'item' and 'npc' (the NPC it came from).")
        payload.update({"item_name": item, "npc_name": npc})
        quantity = _int(body, "quantity", 1, 2 ** 31 - 1)
        if quantity is not None:
            payload["quantity"] = quantity
        value = _int(body, "value", 0, 2 ** 31 - 1)
        if value is not None:
            payload["value"] = value
        kc = _int(body, "kc", 0)
        if kc is not None:
            payload["kill_count"] = kc
    elif sub_type == "clog":
        if not item:
            raise _bad("A collection log submission needs 'item'.")
        payload["item_name"] = item
        if npc:
            payload["source"] = npc
        kc = _int(body, "kc", 0)
        if kc is not None:
            payload["kc"] = kc
    elif sub_type == "pet":
        if not item:
            raise _bad("A pet submission needs 'item' (the pet's name).")
        payload["pet_name"] = item
        if npc:
            payload["source"] = npc
        kc = _int(body, "kc", 0)
        if kc is not None:
            payload["killcount"] = kc
    elif sub_type == "pb":
        if not npc:
            raise _bad("A personal best needs 'npc' (the boss).")
        time_ms = parse_time_ms(body.get("time"))
        if time_ms is None:
            time_ms = _int(body, "time_ms", 1)
        if not time_ms or time_ms <= 0:
            raise _bad("A personal best needs 'time' (like 1:23.4) or 'time_ms'.")
        payload.update({"npc_name": npc, "boss_name": npc, "time_ms": int(time_ms)})
        team_size = _int(body, "team_size", 1, 100)
        if team_size is not None:
            payload["team_size"] = team_size
    elif sub_type == "ca":
        task = _text(body, "task", required=True)
        tier = (_text(body, "tier", required=True) or "").lower()
        if tier not in CA_TIERS:
            raise _bad(f"'tier' must be one of {', '.join(CA_TIERS)}.")
        payload.update({"task": task, "tier": tier})
    return sub_type, payload


def parse_api_fields(body: dict) -> dict:
    """The optional per-call fields: idempotency key, note, drop time and
    the duplicate check switch."""
    fields = {
        "api_external_id": _text(body, "external_id", max_len=64),
        "api_note": _text(body, "note", max_len=500),
        "api_skip_if_tracked": _bool(body, "skip_if_tracked", True),
    }
    obtained = _int(body, "obtained_at", 0, 2 ** 53)
    if obtained is not None:
        if obtained > 10 ** 11:  # milliseconds, from a JS Date.now()
            obtained //= 1000
        fields["api_obtained_at"] = obtained
    return fields


def check_image_url(url: str) -> str:
    """Only https links on the Discord CDN; returns the url or raises."""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise _bad("'image_url' is not a valid URL.")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in IMAGE_URL_HOSTS or parsed.port not in (None, 443):
        raise _bad("'image_url' must be a Discord attachment link "
                   "(cdn.discordapp.com or media.discordapp.net). "
                   "Upload any other image as the 'image' file instead.")
    return url


def check_image_bytes(raw: bytes) -> tuple[str, str]:
    """``(extension, content_type)`` for a real PNG/JPEG/WebP/GIF, or raise.
    The format comes from the bytes, never from what the caller declared."""
    if not raw:
        raise _bad("The image was empty.")
    if len(raw) > IMAGE_MAX_BYTES:
        raise _bad("Images are capped at 10 MB.")
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(raw))
        fmt_name = image.format
        image.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise _bad("The image must be a PNG, JPEG, WebP or GIF.")
    fmt = IMAGE_FORMATS.get(fmt_name or "")
    if fmt is None:
        raise _bad("The image must be a PNG, JPEG, WebP or GIF.")
    return fmt


def intake_response(status_code: int, data) -> tuple[int, dict]:
    """Map the intake's answer to ours: ``(status, body)``."""
    if not isinstance(data, dict):
        data = {}
    if status_code in (401, 503) or status_code >= 500:
        return 502, {"error": "pipeline_unavailable",
                     "detail": "The submission pipeline is unavailable. Try again shortly."}
    if status_code >= 400:
        return 422, {"error": "invalid_submission",
                     "detail": data.get("error") or data.get("message") or "Rejected."}
    status = data.get("status")
    body = {
        "id": data.get("api_submission_id"),
        "status": status,
        "message": data.get("message"),
        "notice": data.get("notice"),
        "matched_drop_id": data.get("matched_drop_id"),
        "replayed": bool(data.get("replayed")),
    }
    if status in ("accepted", "already_tracked"):
        return 200, body
    if status == "rejected":
        return 422, {**body, "error": "rejected",
                     "detail": data.get("message") or "The submission was rejected."}
    if status == "processing":
        # A retry of a call that is still running (or died mid-way). Its
        # outcome lands on GET /v2/submissions/{id}; a call that never
        # finished needs a new external_id.
        return 409, {**body, "error": "in_progress",
                     "detail": "A submission with this external_id is still being "
                               "processed. Check GET /v2/submissions/{id}."}
    return 502, {**body, "error": "pipeline_error",
                 "detail": data.get("message") or data.get("error")
                 or "The submission could not be processed."}


# ── DB reads (worker thread) ─────────────────────────────────────────────────

def _resolve_member(session, group_id: int, ref) -> tuple[int, str] | None:
    """``(player_id, player_name)`` when ``ref`` (id or exact RSN) names a
    player in ``group_id``; None otherwise — not existing and not being a
    member look identical, like every other group-scoped read here."""
    from sqlalchemy import text

    from data_api import scope

    player_id = scope.resolve_player_ref(session, str(ref))
    if player_id is None:
        return None
    row = session.execute(
        text(
            "SELECT p.player_id, p.player_name FROM players p "
            "JOIN user_group_association uga ON uga.player_id = p.player_id "
            "WHERE p.player_id = :pid AND uga.group_id = :gid LIMIT 1"
        ),
        {"pid": int(player_id), "gid": int(group_id)},
    ).first()
    return (int(row[0]), str(row[1])) if row else None


def _submit_rate_ok(key_id: int) -> bool:
    """The per-key submissions-per-minute ceiling (fails open on Redis)."""
    try:
        from utils.redis import RedisClient

        conn = RedisClient().client
        bucket = f"dataapi:submit:{int(key_id)}:{int(time.time()) // 60}"
        pipe = conn.pipeline()
        pipe.incr(bucket, 1)
        pipe.expire(bucket, 120)
        used = pipe.execute()[0]
        return int(used) <= SUBMISSIONS_PER_MIN
    except Exception:
        return True


async def _read_body() -> tuple[dict, bytes | None]:
    """``(fields, image_bytes)`` from a JSON or multipart body."""
    content_type = request.headers.get("Content-Type", "")
    if "multipart/form-data" in content_type:
        form = await request.form
        files = await request.files
        fields = {k: form[k] for k in form}
        upload = files.get("image") if files else None
        return fields, (upload.read() if upload is not None else None)
    try:
        body = await request.get_json(force=True, silent=True)
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise SubmissionError(400, "malformed_body",
                              "Send a JSON object, or multipart/form-data with an 'image' file.")
    return body, None


async def _fetch_image(url: str) -> bytes:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise _bad(f"Couldn't download 'image_url' (HTTP {resp.status_code}). "
                               "Discord links expire; send a fresh one or upload the file.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > IMAGE_MAX_BYTES:
                        raise _bad("Images are capped at 10 MB.")
                    chunks.append(chunk)
                return b"".join(chunks)
    except SubmissionError:
        raise
    except Exception:
        raise _bad("Couldn't download 'image_url'. Send a fresh link or upload the file.")


async def _forward(payload: dict, image: tuple[bytes, str, str] | None):
    import httpx

    key = (os.getenv("MANUAL_SUBMIT_KEY") or "").strip()
    headers = {MANUAL_SUBMIT_KEY_HEADER: key}
    url = f"{INTAKE_API_URL}/manual-submit"
    async with httpx.AsyncClient(timeout=INTAKE_TIMEOUT_SECONDS) as client:
        if image is None:
            resp = await client.post(url, json=payload, headers=headers)
        else:
            raw, ext, content_type = image
            form = {k: ("true" if v is True else "false" if v is False else str(v))
                    for k, v in payload.items() if v is not None}
            resp = await client.post(
                url, data=form, headers=headers,
                files={"image_file": (f"proof.{ext}", raw, content_type)})
    try:
        data = resp.json()
    except Exception:
        data = {}
    return resp.status_code, data


# ── routes ───────────────────────────────────────────────────────────────────

_NOT_ALLOWED = {
    "error": "forbidden",
    "detail": "This key cannot submit. Submissions need a group key with "
              "submissions enabled; ask DropTracker staff.",
}


@submissions_bp.route("/submissions", methods=["POST"])
async def create_submission():
    key = g.api_key
    started = time.perf_counter()
    endpoint = "submissions.create"

    def done(status: int, body: dict, headers: dict | None = None, cost: int = 0):
        usage.record(key["key_id"], endpoint, status,
                     (time.perf_counter() - started) * 1000, cost,
                     limited=status == 429)
        return _json(body, status, headers)

    if key.get("scope") != "group" or not key.get("can_submit") or not key.get("group_id"):
        return done(403, _NOT_ALLOWED)

    try:
        body, upload = await _read_body()
        sub_type, payload = build_intake_payload(body)
        payload.update(parse_api_fields(body))
        # Explicit None checks: player id 0 is a real id, not "missing".
        player_ref = body.get("player")
        if player_ref is None or player_ref == "":
            player_ref = body.get("player_id")
        if player_ref is None or player_ref == "" or isinstance(player_ref, (bool, dict, list)):
            raise _bad("'player' is required (an RSN or a DropTracker player id).")
        image_url = _text(body, "image_url", max_len=2000)
        if upload is not None and image_url:
            raise _bad("Send either an 'image' file or an 'image_url', not both.")
        if image_url:
            check_image_url(image_url)
    except SubmissionError as err:
        return done(err.status, {"error": err.error, "detail": err.detail})

    decision = await asyncio.to_thread(
        check_and_charge, key["key_id"], key["limits"], SUBMISSION_COST)
    if not decision.allowed:
        headers = dict(decision.headers)
        if decision.retry_after:
            headers["Retry-After"] = str(decision.retry_after)
        return done(429, {"error": "rate_limited", "limit": decision.reason,
                          "detail": f"Exceeded the {decision.reason} budget for this key."},
                    headers)
    if not await asyncio.to_thread(_submit_rate_ok, key["key_id"]):
        return done(429, {"error": "rate_limited", "limit": "submissions_per_min",
                          "detail": f"At most {SUBMISSIONS_PER_MIN} submissions a minute per key."},
                    {"Retry-After": str(60 - int(time.time()) % 60)})

    with Concurrency(key["key_id"], key["limits"]["max_concurrency"]) as slot:
        if not slot.acquired:
            return done(429, {"error": "too_many_concurrent_requests",
                              "detail": "This key already has its maximum number of "
                                        "requests in flight."}, {"Retry-After": "1"})

        def _lookup():
            session = SessionLocal()
            try:
                return _resolve_member(session, key["group_id"], player_ref)
            finally:
                session.close()

        try:
            member = await asyncio.to_thread(_lookup)
        except Exception as exc:
            if is_statement_timeout(exc):
                return done(503, {"error": "query_too_heavy", "detail": "Try again shortly."})
            raise
        if member is None:
            return done(404, {"error": "not_found",
                              "detail": "No player by that name or id is a member of this group. "
                                        "Players appear once they have used the DropTracker plugin."})
        _player_id, player_name = member

        image = None
        try:
            raw = upload if upload is not None else (
                await _fetch_image(image_url) if image_url else None)
            if raw is not None:
                ext, content_type = await asyncio.to_thread(check_image_bytes, raw)
                image = (raw, ext, content_type)
        except SubmissionError as err:
            return done(err.status, {"error": err.error, "detail": err.detail})

        payload.update({
            "player_name": player_name,
            "api_key_id": int(key["key_id"]),
            "api_group_id": int(key["group_id"]),
            "api_preapproved": bool(key.get("submit_preapproved")),
        })
        try:
            status_code, data = await _forward(payload, image)
        except Exception:
            status_code, data = 503, {}
        status, out = intake_response(status_code, data)
        out["type"] = sub_type
        out["player"] = player_name
        out["preapproved"] = bool(key.get("submit_preapproved"))
        headers = dict(decision.headers)
        headers["Cache-Control"] = "no-store"
        return done(status, out, headers, cost=SUBMISSION_COST)


def _row_body(row, drop_id, credits) -> dict:
    def iso(value):
        return value.isoformat() + "Z" if value is not None else None

    return {
        "id": int(row.id),
        "external_id": row.external_id,
        "status": row.status,
        "message": row.message,
        "type": {v: k for k, v in TYPE_MAP.items()}.get(row.submission_type, row.submission_type),
        "player_id": int(row.player_id),
        "item": row.item_name,
        "preapproved": bool(row.preapproved),
        "matched_drop_id": row.matched_drop_id,
        "drop_id": drop_id,
        "image_url": row.image_url,
        "note": row.note,
        "obtained_at": iso(row.obtained_at),
        "created_at": iso(row.created_at),
        "event_credits": credits,
    }


def _event_credits(session, player_id: int, guid: str | None) -> list:
    """Ledger rows the events engine wrote for this submission. Engine guids
    can carry a ``#p<n>``/``#b<n>`` suffix (one row per path or bonus)."""
    if not guid:
        return []
    from sqlalchemy import bindparam, text

    event_ids = [r[0] for r in session.execute(
        text("SELECT DISTINCT event_id FROM web_event_team_members WHERE player_id = :pid"),
        {"pid": int(player_id)},
    ).fetchall()]
    if not event_ids:
        return []
    rows = session.execute(
        text(
            "SELECT c.event_id, e.name, c.task_id, t.label, c.status, c.quantity "
            "FROM web_event_completions c "
            "JOIN web_event_tasks t ON t.id = c.task_id "
            "JOIN web_events e ON e.id = c.event_id "
            "WHERE c.event_id IN :eids AND c.player_id = :pid "
            "AND (c.submission_guid = :guid OR c.submission_guid LIKE :prefix) "
            "ORDER BY c.id LIMIT 50"
        ).bindparams(bindparam("eids", expanding=True)),
        {"eids": event_ids, "pid": int(player_id), "guid": guid[:64],
         "prefix": guid[:60] + "#%"},
    ).fetchall()
    return [{"event_id": r[0], "event": r[1], "task_id": r[2], "task": r[3],
             "status": r[4], "quantity": int(r[5] or 0)} for r in rows]


@submissions_bp.route("/submissions/<int:submission_id>", methods=["GET"])
async def get_submission(submission_id: int):
    """One earlier submission by this group's keys: its outcome, the drop it
    created, and the event credit it earned. Event matching runs a moment
    after the submission returns, so poll this rather than the POST."""
    key = g.api_key
    if key.get("scope") != "group" or not key.get("group_id"):
        return _json(_NOT_ALLOWED, 403)

    def build(session):
        from sqlalchemy import text

        from db.models import ApiSubmission

        row = (session.query(ApiSubmission)
               .filter(ApiSubmission.id == submission_id,
                       ApiSubmission.group_id == int(key["group_id"]))
               .first())
        if row is None:
            return None
        drop_id = None
        if row.submission_type == "drop" and row.submission_guid:
            found = session.execute(
                text("SELECT drop_id FROM drops WHERE unique_id = :g LIMIT 1"),
                {"g": row.submission_guid},
            ).first()
            drop_id = int(found[0]) if found else None
        return _row_body(row, drop_id, _event_credits(session, row.player_id, row.submission_guid))

    from data_api.serving import serve_fixed

    return await serve_fixed("submissions.get", 5, build,
                             not_found_detail="No submission with that id for this group.",
                             cache_seconds=0)


@submissions_bp.route("/submissions", methods=["GET"])
async def list_submissions():
    """This group's recent API submissions, newest first, cursor-paginated
    by id (``?cursor=<last id>``). For a bot to reconcile what it sent."""
    key = g.api_key
    if key.get("scope") != "group" or not key.get("group_id"):
        return _json(_NOT_ALLOWED, 403)
    from data_api.serving import int_param, serve_fixed

    limit, error = int_param("limit", 25, 1, 100)
    if error is not None:
        return error
    cursor, error = int_param("cursor", 0, 0, 2 ** 63 - 1)
    if error is not None:
        return error

    def build(session):
        from db.models import ApiSubmission

        query = (session.query(ApiSubmission)
                 .filter(ApiSubmission.group_id == int(key["group_id"])))
        if cursor:
            query = query.filter(ApiSubmission.id < cursor)
        rows = query.order_by(ApiSubmission.id.desc()).limit(limit).all()
        items = [_row_body(r, None, None) for r in rows]
        for item in items:
            item.pop("drop_id", None)
            item.pop("event_credits", None)
        return {"count": len(items), "submissions": items,
                "next_cursor": items[-1]["id"] if len(items) == limit else None}

    return await serve_fixed("submissions.list", 5, build, cache_seconds=0)
