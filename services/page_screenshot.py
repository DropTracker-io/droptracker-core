"""Full-page PNG screenshots of a URL via headless chromium over CDP.

The board-image feature captures the REAL web board (a chrome-less page at
``/board-image/{id}``) at 1:1 instead of re-drawing it. chromium's CLI
``--screenshot`` only grabs the viewport, so we drive the browser over the Chrome
DevTools Protocol and use ``Page.captureScreenshot{captureBeyondViewport:true}``
for a full-page, ``deviceScaleFactor``-crisp capture at any height.

Uses the **system** chromium (no bundled browser download) and the already
vendored ``websockets`` client — no new dependency. Like the SVG rasterizer it
sets a writable ``HOME`` in the child env so it works under the core bot's
systemd ``ProtectHome=true`` sandbox (see
``services.boardgame_generator._rasterize_sync``).
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

import httpx
import websockets

from db.app_logger import AppLogger

app_logger = AppLogger()

_CHROMIUM = os.getenv("CHROMIUM_BIN", "/usr/bin/chromium")

# JS readiness probe: DOM parsed and every <img> settled (loaded or errored).
_READY_JS = (
    "(function(){try{return document.readyState==='complete'"
    "&&Array.from(document.images).every(function(i){"
    "return i.complete&&(i.naturalWidth>0||i.src==='');});}"
    "catch(e){return false;}})()"
)


class _CDP:
    """Minimal Chrome DevTools Protocol session over one websocket.

    A background reader fans incoming frames into per-id response futures and a
    shared event queue; :meth:`send` awaits its response, :meth:`wait_event`
    pulls the next matching event (earlier events buffer, so we never miss one
    fired before we start waiting)."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif "method" in msg:
                    await self._events.put(msg)
        except Exception:
            pass

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None, timeout: float = 20.0) -> dict:
        self._id += 1
        mid = self._id
        cmd = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            cmd["sessionId"] = session_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(cmd))
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"CDP {method} failed: {msg['error']}")
        return msg.get("result", {})

    async def wait_event(self, method: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"timed out waiting for {method}")
            msg = await asyncio.wait_for(self._events.get(), timeout=remaining)
            if msg.get("method") == method:
                return msg

    async def close(self):
        self._reader.cancel()


def _launch(profile_dir: str, home: str) -> subprocess.Popen:
    cmd = [
        _CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--force-color-profile=srgb",
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "about:blank",
    ]
    # chromium needs a WRITABLE $HOME for its profile/crashpad DB; under systemd
    # ProtectHome=true the inherited HOME is inaccessible (see boardgame_generator).
    env = {**os.environ, "HOME": home}
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


async def _devtools_port(profile_dir: str, proc: subprocess.Popen,
                         timeout: float) -> int:
    """The debug port chromium chose (--remote-debugging-port=0), read from the
    DevToolsActivePort file it writes into the profile dir."""
    path = os.path.join(profile_dir, "DevToolsActivePort")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"chromium exited early (code {proc.returncode})")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                line = fh.readline().strip()
            if line:
                return int(line)
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("chromium DevTools port not ready in time")


async def _browser_ws_url(port: int) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/json/version")
        resp.raise_for_status()
        return resp.json()["webSocketDebuggerUrl"]


async def _capture(ws_url: str, url: str, *, width: int, scale: float,
                   timeout: float, max_height: int,
                   viewport_height: int) -> bytes:
    # max_size=None: a full-page base64 PNG easily exceeds the 1 MiB ws default.
    async with websockets.connect(ws_url, max_size=None,
                                  open_timeout=10) as ws:
        cdp = _CDP(ws)
        try:
            target = await cdp.send("Target.createTarget", {"url": "about:blank"})
            sid = (await cdp.send(
                "Target.attachToTarget",
                {"targetId": target["targetId"], "flatten": True}
            ))["sessionId"]

            await cdp.send("Page.enable", session_id=sid)
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": viewport_height,
                "deviceScaleFactor": scale,
                "mobile": False,
            }, session_id=sid)

            await cdp.send("Page.navigate", {"url": url}, session_id=sid)
            try:
                await cdp.wait_event("Page.loadEventFired", timeout=timeout)
            except asyncio.TimeoutError:
                pass  # fall through to the readiness poll / capture anyway

            # Poll until the DOM + images have settled, then let webfonts finish.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                res = await cdp.send("Runtime.evaluate", {
                    "expression": _READY_JS, "returnByValue": True,
                }, session_id=sid)
                if res.get("result", {}).get("value") is True:
                    break
                await asyncio.sleep(0.15)
            try:
                await cdp.send("Runtime.evaluate", {
                    "expression": "document.fonts && document.fonts.ready"
                                  ".then(function(){return true;})",
                    "awaitPromise": True, "returnByValue": True,
                }, session_id=sid, timeout=5)
            except Exception:
                pass
            await asyncio.sleep(0.2)  # paint settle

            metrics = await cdp.send("Page.getLayoutMetrics", session_id=sid)
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            content_h = int(math.ceil(size.get("height") or 0)) or viewport_height
            content_h = min(content_h, max_height)
            content_w = int(math.ceil(size.get("width") or width)) or width

            shot = await cdp.send("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": min(content_w, width),
                         "height": content_h, "scale": 1},
            }, session_id=sid, timeout=timeout)
            return base64.b64decode(shot["data"])
        finally:
            await cdp.close()


async def screenshot_url(url: str, *, width: int = 1100, scale: float = 2.0,
                         timeout: float = 30.0, max_height: int = 8000,
                         viewport_height: int = 1080) -> bytes:
    """Render ``url`` in headless chromium and return a full-page PNG.

    ``width`` is the CSS layout width; ``scale`` is the device pixel ratio (2 =
    retina-crisp). Raises on failure — callers that must fail open
    (:mod:`services.event_board_image`) wrap this in try/except.

    ``viewport_height`` is the emulated window height, and the floor on the
    captured height: chromium reports ``cssContentSize`` as at least the
    viewport, so a page shorter than the window yields a PNG padded with body
    background to the bottom. Callers rendering a fixed-size artifact (the recap
    poster) pass a short viewport to get an exactly-cropped image. Keep it tall
    enough to cover anything lazily loaded — nothing below the fold will have
    started fetching."""
    tmp = tempfile.mkdtemp(prefix="dt-shot-")
    proc = None
    try:
        profile = os.path.join(tmp, "profile")
        proc = _launch(profile, tmp)
        port = await _devtools_port(profile, proc, timeout=15)
        ws_url = await _browser_ws_url(port)
        return await _capture(ws_url, url, width=width, scale=scale,
                              timeout=timeout, max_height=max_height,
                              viewport_height=viewport_height)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
