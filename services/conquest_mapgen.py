"""Regenerate the Gielinor Conquest board for an organiser's selection.

When a preset leaves regions or tiles out, the board is redrawn without them
(scripts/conquest_map/variant.py): a removed region's land goes to the kept
regions on its landmass, a landmass with nothing kept is dropped and the frame
is cropped, and the seas are re-cut. That needs scipy/scikit-image (not in the
service venv) and ~20 s of CPU, so it runs as a niced subprocess of the web
API with a private toolchain (``$CONQUEST_MAPGEN_HOME``, default
``<repo>/.conquest_mapgen``: ``pylib/``, ``world.png``; see its README).

Results are cached per selection (``cache/<key>.json``, the terrain WebP on
B2), so a selection is drawn once. One generation runs at a time across the
web API's workers (a Redis lock); a request that finds it busy is told
"queued" and asks again. The full selection never gets here: it uses the
committed art pack (services/conquest_art/gielinor.json).

Status per selection: ``ready`` (cached), ``generating``, ``queued``,
``failed`` (for a few minutes, then it may be retried) or ``unavailable``
(no toolchain on this box).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from typing import Iterable, Optional

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.environ.get("CONQUEST_MAPGEN_HOME") or os.path.join(_ROOT, ".conquest_mapgen")
SCRIPT = os.path.join(_ROOT, "scripts", "conquest_map", "variant.py")
PYTHON = "/usr/bin/python3"
# Bump when the generator changes what it draws: cached maps are keyed by it.
ART_VERSION = 3
TIMEOUT_SECONDS = 300
LOCK_KEY = "conquest:mapgen:busy"
STATUS_KEY = "conquest:mapgen:status:{key}"
LOCK_TTL = TIMEOUT_SECONDS + 60
FAILED_TTL = 60
B2_PREFIX = "dt_uploads/conquest/maps"

# Generations this process started and is still running (key -> task).
_running: dict = {}


def selection_key(regions: Iterable[str], drop: Iterable[str]) -> str:
    raw = json.dumps({"v": ART_VERSION, "regions": sorted(set(regions)),
                      "drop": sorted(set(drop))}, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _cache_path(key: str) -> str:
    return os.path.join(HOME, "cache", f"{key}.json")


def toolchain_ready() -> bool:
    return (os.path.isdir(os.path.join(HOME, "pylib"))
            and os.path.exists(os.path.join(HOME, "world.png"))
            and os.path.exists(SCRIPT) and shutil.which("chromium", path="/usr/bin") is not None)


def cached_art(key: str) -> Optional[dict]:
    try:
        with open(_cache_path(key)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _redis():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def status(key: str) -> str:
    if cached_art(key) is not None:
        return "ready"
    if key in _running:
        return "generating"
    r = _redis()
    if r is not None:
        try:
            raw = r.get(STATUS_KEY.format(key=key))
        except Exception:
            raw = None
        if raw:
            return raw.decode() if isinstance(raw, bytes) else str(raw)
    return "missing"


async def ensure_art(regions: Iterable[str], drop: Iterable[str]) -> tuple:
    """``(status, art)``: the cached art pack for this selection, or start
    (or keep waiting on) its generation. Never blocks on the generation."""
    regions, drop = sorted(set(regions)), sorted(set(drop))
    key = selection_key(regions, drop)
    art = cached_art(key)
    if art is not None:
        return "ready", art
    if not toolchain_ready():
        return "unavailable", None
    current = status(key)
    if current in ("generating", "failed"):
        return current, None
    r = _redis()
    if r is not None:
        try:
            if not r.set(LOCK_KEY, key, nx=True, ex=LOCK_TTL):
                holder = r.get(LOCK_KEY)
                holder = holder.decode() if isinstance(holder, bytes) else holder
                return ("generating" if holder == key else "queued"), None
            r.set(STATUS_KEY.format(key=key), "generating", ex=LOCK_TTL)
        except Exception:
            log.exception("conquest mapgen: redis lock failed; running unlocked")
    elif _running:
        return "queued", None
    _running[key] = asyncio.get_running_loop().create_task(_generate(key, regions, drop))
    return "generating", None


async def _generate(key: str, regions: list, drop: list) -> None:
    r = _redis()
    tmp = tempfile.mkdtemp(prefix="conquest-map-")
    try:
        out_json = os.path.join(tmp, "art.json")
        out_webp = os.path.join(tmp, f"gielinor-{key}.webp")
        env = dict(os.environ, PYTHONPATH=os.path.join(HOME, "pylib"))
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", PYTHON, SCRIPT, os.path.join(HOME, "world.png"),
            out_json, out_webp, "--regions", ",".join(regions), "--drop", ",".join(drop),
            env=env, cwd=os.path.dirname(SCRIPT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("the map took too long to draw")
        if proc.returncode != 0:
            tail = (err or b"").decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError("the map generator failed: " + " | ".join(tail))
        with open(out_json) as fh:
            art = json.load(fh)
        with open(out_webp, "rb") as fh:
            webp = fh.read()

        from utils.b2_storage import upload_bytes
        from web_api.routes.submissions import B2_CDN_BASE_URL

        object_key = f"{B2_PREFIX}/{key}.webp"
        await upload_bytes(webp, object_key, "image/webp")
        art["background"] = f"{B2_CDN_BASE_URL.rstrip('/')}/{object_key}"
        art["key"] = key
        os.makedirs(os.path.dirname(_cache_path(key)), exist_ok=True)
        part = _cache_path(key) + ".part"
        with open(part, "w") as fh:
            json.dump(art, fh, separators=(",", ":"))
        os.replace(part, _cache_path(key))
        log.info("conquest mapgen: drew %s (%s)", key, (out or b"").decode().strip())
        if r is not None:
            r.delete(STATUS_KEY.format(key=key))
    except Exception as exc:
        log.exception("conquest mapgen: %s failed", key)
        if r is not None:
            try:
                r.set(STATUS_KEY.format(key=key), "failed", ex=FAILED_TTL)
            except Exception:
                pass
        _failures[key] = str(exc)[:300]
    finally:
        _running.pop(key, None)
        shutil.rmtree(tmp, ignore_errors=True)
        if r is not None:
            try:
                holder = r.get(LOCK_KEY)
                holder = holder.decode() if isinstance(holder, bytes) else holder
                if holder == key:
                    r.delete(LOCK_KEY)
            except Exception:
                pass


# Why the last generation of a selection failed (this process only).
_failures: dict = {}


def failure_reason(key: str) -> Optional[str]:
    return _failures.get(key)
