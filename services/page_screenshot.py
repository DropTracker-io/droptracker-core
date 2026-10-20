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
import atexit
import base64
import ctypes
import fcntl
import json
import math
import os
import shutil
import signal
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
        # --disable-gpu above turns off hardware acceleration, not WebGL: ANGLE
        # still rasterises in software via SwiftShader, which newer chromium
        # refuses to do for WebGL unless explicitly allowed. Without this flag a
        # WebGL page (the character model render) gets a null context and draws
        # nothing, while every other page is unaffected.
        "--enable-unsafe-swiftshader",
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
    # The browser is long-lived now, so it must not outlive us: a worker that
    # is SIGKILLed never runs atexit, and an orphaned chromium keeps its
    # threads forever. PR_SET_PDEATHSIG has the kernel SIGTERM it instead.
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env,
                            preexec_fn=_die_with_parent)


_PR_SET_PDEATHSIG = 1
try:
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):  # not Linux
    _prctl = None


def _die_with_parent() -> None:
    if _prctl is not None:
        _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)


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
                   viewport_height: int, ready_js: str | None = None,
                   transparent: bool = False) -> bytes:
    # max_size=None: a full-page base64 PNG easily exceeds the 1 MiB ws default.
    async with websockets.connect(ws_url, max_size=None,
                                  open_timeout=10) as ws:
        cdp = _CDP(ws)
        ctx = None
        try:
            # A throwaway browser context per render: the browser is shared and
            # long-lived, so cookies, storage and cache from one page must not
            # leak into the next. Disposing the context closes its tab too.
            ctx = (await cdp.send("Target.createBrowserContext",
                                  {"disposeOnDetach": True}))["browserContextId"]
            target = await cdp.send("Target.createTarget",
                                    {"url": "about:blank", "browserContextId": ctx})
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

            if transparent:
                # Chromium paints an opaque white backdrop behind every page, so
                # a page with no background of its own still captures solid, and
                # `captureScreenshot`'s own alpha option cannot see past it.
                # Overriding the default background to fully transparent is the
                # only way to get real alpha out.
                await cdp.send("Emulation.setDefaultBackgroundColorOverride",
                               {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
                               session_id=sid)

            # ``timeout`` bounds load AND readiness together. They used to get
            # one each, so a page that never became ready held its browser (and
            # now a capacity slot) for twice the caller's budget.
            deadline = time.monotonic() + timeout
            await cdp.send("Page.navigate", {"url": url}, session_id=sid)
            try:
                await cdp.wait_event("Page.loadEventFired",
                                     timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass  # fall through to the readiness poll / capture anyway

            # Poll until the DOM + images have settled, then let webfonts finish.
            while time.monotonic() < deadline:
                res = await cdp.send("Runtime.evaluate", {
                    "expression": _READY_JS, "returnByValue": True,
                }, session_id=sid)
                if res.get("result", {}).get("value") is True:
                    if not ready_js:
                        break
                    extra = await cdp.send("Runtime.evaluate", {
                        "expression": ready_js, "returnByValue": True,
                    }, session_id=sid)
                    if extra.get("result", {}).get("value") is True:
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
                "omitBackground": transparent,
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": min(content_w, width),
                         "height": content_h, "scale": 1},
            }, session_id=sid, timeout=timeout)
            return base64.b64decode(shot["data"])
        finally:
            if ctx is not None:
                try:
                    await cdp.send("Target.disposeBrowserContext",
                                   {"browserContextId": ctx}, timeout=5)
                except Exception:
                    pass
            await cdp.close()


# ---------------------------------------------------------------------------
# Capacity.
#
# Rendering is CPU-heavy (the character model is WebGL drawn in software by
# SwiftShader) and every service on the box runs chromium under one user's
# LimitNPROC=4096. On 2026-10-20 a login burst put ~24 one-shot browsers (~80
# threads each) in flight across the intake API's workers and it died with
# "can't start new thread". Three limits keep that from recurring:
#
# * one long-lived browser per process, reused for every render (a fresh
#   browser per render cost ~1s of start-up and a full thread set each time);
# * a per-process cap on concurrent renders (tabs) in that browser;
# * a box-wide cap shared by every service, held as flock()ed slot files. The
#   kernel drops a lock when its holder dies, so a crashed process can never
#   leak a slot. The files live under /store/droptracker because every unit
#   has PrivateTmp=true, so /tmp is not shared between services.
#
# Waiting for capacity is bounded: past SCREENSHOT_QUEUE_TIMEOUT the call raises
# ScreenshotBusy. Every caller already fails open (no picture, never a lost
# notification), and a backlog that cannot drain is better shed than queued.
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except ValueError:
        return default


_MAX_CONCURRENT = _env_int("SCREENSHOT_MAX_CONCURRENCY", 2)
_GLOBAL_SLOTS = _env_int("SCREENSHOT_GLOBAL_SLOTS", 8, minimum=0)  # 0 = off
_QUEUE_TIMEOUT = float(_env_int("SCREENSHOT_QUEUE_TIMEOUT", 60))
_RECYCLE_AFTER = _env_int("SCREENSHOT_RECYCLE_AFTER", 200)
_IDLE_SECONDS = float(_env_int("SCREENSHOT_IDLE_SECONDS", 120))
_SLOT_DIR = os.getenv(
    "SCREENSHOT_SLOT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "var", "screenshot-slots"),
)


class ScreenshotBusy(RuntimeError):
    """No render capacity came free within the queue timeout."""


def _try_global_slot() -> Optional[int] | bool:
    """A locked slot fd, None when every slot is taken, or True when the
    box-wide cap is off or unusable (fail open to the per-process cap)."""
    if _GLOBAL_SLOTS <= 0:
        return True
    try:
        if not os.path.isdir(_SLOT_DIR):
            os.makedirs(_SLOT_DIR, exist_ok=True)
            try:
                # Two service accounts render (user, debian): both must be able
                # to create and lock slot files here.
                os.chmod(_SLOT_DIR, 0o1777)
            except OSError:
                pass
    except OSError:
        return True
    usable = 0
    for i in range(_GLOBAL_SLOTS):
        path = os.path.join(_SLOT_DIR, f"slot-{i}")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
        except OSError:
            continue
        usable += 1
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass  # not ours; read-only access is enough for flock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            os.close(fd)
    return None if usable else True


def _release_global_slot(fd) -> None:
    if isinstance(fd, int) and not isinstance(fd, bool):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def _acquire_global_slot(deadline: float):
    delay = 0.05
    while True:
        fd = _try_global_slot()
        if fd is not None:
            return fd
        if time.monotonic() >= deadline:
            raise ScreenshotBusy("no box-wide screenshot slot came free in time")
        await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 0.5)


class _Browser:
    """One running chromium and the bookkeeping to retire it."""

    def __init__(self, proc, tmp, ws_url):
        self.proc = proc
        self.tmp = tmp
        self.ws_url = ws_url
        self.renders = 0
        self.in_use = 0
        self.failures = 0  # consecutive
        self.retiring = False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


#: Consecutive failed renders after which a browser that is still running is
#: presumed wedged and replaced. One failure is usually the page (a timeout, a
#: 5xx from the site), not the browser.
_FAILURES_BEFORE_REPLACE = 2


class _Pool:
    """The per-event-loop browser: launched on demand, shared by concurrent
    renders, replaced after _RECYCLE_AFTER renders, on death, or after repeated
    failures, and shut down after _IDLE_SECONDS without work so a rarely
    rendering process holds no browser at all. A replaced browser finishes the
    renders already running in it and is stopped when the last one ends."""

    def __init__(self):
        self.sem = asyncio.Semaphore(_MAX_CONCURRENT)
        self.lock = asyncio.Lock()
        self.browser: Optional[_Browser] = None
        self.active = 0
        self.idle_task: Optional[asyncio.Task] = None

    def _detach(self, b: _Browser) -> None:
        """Stop handing ``b`` out; caller retires it once nothing uses it."""
        b.retiring = True
        if self.browser is b:
            self.browser = None

    async def checkout(self) -> _Browser:
        async with self.lock:
            if self.idle_task is not None:
                self.idle_task.cancel()
                self.idle_task = None
            b = self.browser
            if b is not None and (not b.alive() or b.renders >= _RECYCLE_AFTER):
                self._detach(b)
                if b.in_use == 0:
                    await _retire(b)
                b = None
            if b is None:
                b = self.browser = await _start_browser()
            b.renders += 1
            b.in_use += 1
            self.active += 1
            return b

    async def checkin(self, b: _Browser, failed: bool) -> None:
        self.active -= 1
        b.in_use -= 1
        b.failures = b.failures + 1 if failed else 0
        if not b.alive() or b.failures >= _FAILURES_BEFORE_REPLACE:
            self._detach(b)
        if b.retiring and b.in_use == 0:
            await _retire(b)
        if self.active == 0 and self.browser is not None and self.idle_task is None:
            self.idle_task = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self):
        try:
            await asyncio.sleep(_IDLE_SECONDS)
        except asyncio.CancelledError:
            return
        async with self.lock:
            self.idle_task = None
            b = self.browser
            if self.active == 0 and b is not None:
                self._detach(b)
                await _retire(b)


_pools: dict = {}
_all_browsers: set = set()


def _pool() -> _Pool:
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        # A previous loop (scripts call asyncio.run() repeatedly) can no longer
        # run its pool's coroutines; stop its browser synchronously.
        for old in list(_pools.values()):
            if old.browser is not None:
                _stop_browser_sync(old.browser)
        _pools.clear()
        pool = _pools[loop] = _Pool()
    return pool


async def _start_browser() -> _Browser:
    tmp = tempfile.mkdtemp(prefix="dt-shot-")
    proc = None
    try:
        profile = os.path.join(tmp, "profile")
        proc = _launch(profile, tmp)
        port = await _devtools_port(profile, proc, timeout=15)
        ws_url = await _browser_ws_url(port)
    except BaseException:
        await _off_loop_teardown(proc, tmp)
        raise
    b = _Browser(proc, tmp, ws_url)
    _all_browsers.add(b)
    return b


async def _retire(b: _Browser) -> None:
    _all_browsers.discard(b)
    await _off_loop_teardown(b.proc, b.tmp)


def _stop_browser_sync(b: _Browser) -> None:
    _all_browsers.discard(b)
    _teardown(b.proc, b.tmp)


async def _off_loop_teardown(proc, tmp) -> None:
    # Chromium needs ~100ms to exit after SIGTERM on a quiet box and far longer
    # under load, and proc.wait() is a blocking syscall: run inline it stalled
    # the worker's event loop. Off the loop, with the same inline fallback if
    # the loop is already shutting down and can no longer hand work to a thread.
    try:
        await asyncio.to_thread(_teardown, proc, tmp)
    except RuntimeError:
        _teardown(proc, tmp)


@atexit.register
def _stop_all_browsers() -> None:
    for b in list(_all_browsers):
        _stop_browser_sync(b)


async def screenshot_url(url: str, *, width: int = 1100, scale: float = 2.0,
                         timeout: float = 30.0, max_height: int = 8000,
                         viewport_height: int = 1080,
                         ready_js: str | None = None,
                         transparent: bool = False,
                         queue_timeout: float | None = None) -> bytes:
    """Render ``url`` in headless chromium and return a full-page PNG.

    ``width`` is the CSS layout width; ``scale`` is the device pixel ratio (2 =
    retina-crisp). Raises on failure — callers that must fail open
    (:mod:`services.event_board_image`) wrap this in try/except.
    :class:`ScreenshotBusy` means no capacity came free within
    ``queue_timeout`` seconds (default ``SCREENSHOT_QUEUE_TIMEOUT``).

    ``transparent`` keeps the PNG's alpha channel: chromium otherwise paints an
    opaque backdrop, so a page with no background of its own still captures
    solid. Needed for anything composited onto an unknown background — a Discord
    client can be light or dark, and a baked-in dark rectangle looks broken on
    the other one.

    ``ready_js`` is an optional JavaScript expression polled alongside the
    built-in probe; the capture waits for it to return true. The built-in probe
    only knows about the DOM and ``<img>`` elements, so anything drawn
    asynchronously into a canvas (the WebGL character render) must signal its
    own readiness or it will be photographed blank.

    ``viewport_height`` is the emulated window height, and the floor on the
    captured height: chromium reports ``cssContentSize`` as at least the
    viewport, so a page shorter than the window yields a PNG padded with body
    background to the bottom. Callers rendering a fixed-size artifact (the recap
    poster) pass a short viewport to get an exactly-cropped image. Keep it tall
    enough to cover anything lazily loaded — nothing below the fold will have
    started fetching."""
    pool = _pool()
    asked = time.monotonic()
    deadline = asked + (_QUEUE_TIMEOUT if queue_timeout is None else queue_timeout)
    outcome = "busy"
    started = None
    try:
        try:
            await asyncio.wait_for(pool.sem.acquire(),
                                   timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            raise ScreenshotBusy("no screenshot slot in this process came free in time")
        try:
            slot = await _acquire_global_slot(deadline)
            try:
                browser = await pool.checkout()
                started = time.monotonic()
                outcome = "failed"
                try:
                    png = await _capture(browser.ws_url, url, width=width, scale=scale,
                                         timeout=timeout, max_height=max_height,
                                         viewport_height=viewport_height,
                                         ready_js=ready_js, transparent=transparent)
                    outcome = "ok"
                    return png
                finally:
                    await pool.checkin(browser, outcome != "ok")
            finally:
                _release_global_slot(slot)
        finally:
            pool.sem.release()
    finally:
        _stats.record(outcome, (started or time.monotonic()) - asked,
                      time.monotonic() - started if started else None)


class _Stats:
    """Per-process render counters, logged once a minute while there is work:
    the only way to tell from the journal whether the caps are too tight
    (``busy``/long ``wait``) or renders are slow (``render``)."""

    INTERVAL = 60.0

    def __init__(self):
        self._reset(time.monotonic())

    def _reset(self, now):
        self.since = now
        self.counts = {"ok": 0, "failed": 0, "busy": 0}
        self.render_total = 0.0
        self.render_max = 0.0
        self.wait_max = 0.0

    def record(self, outcome, waited, rendered):
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        self.wait_max = max(self.wait_max, waited)
        if rendered is not None:
            self.render_total += rendered
            self.render_max = max(self.render_max, rendered)
        now = time.monotonic()
        if now - self.since >= self.INTERVAL:
            done = self.counts["ok"] + self.counts["failed"]
            print(f"[screenshot] pid={os.getpid()} {now - self.since:.0f}s: "
                  f"ok={self.counts['ok']} failed={self.counts['failed']} "
                  f"busy={self.counts['busy']} "
                  f"render_avg={self.render_total / done if done else 0:.1f}s "
                  f"render_max={self.render_max:.1f}s wait_max={self.wait_max:.1f}s",
                  flush=True)
            self._reset(now)


_stats = _Stats()


def _teardown(proc: subprocess.Popen | None, tmp: str) -> None:
    """Stop the browser and drop its profile dir. Blocking; call off-loop."""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    shutil.rmtree(tmp, ignore_errors=True)
