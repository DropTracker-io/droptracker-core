"""Server-rendered pictures of a player's character.

Two of them, from one model and one component:

* the **still** — a tall full-body shot, for Discord notifications;
* the **avatar** — a square head-and-shoulders crop that stands in for the
  letter tile wherever the site lists a player.

Mirrors :mod:`services.recap_image`: build a URL for a chrome-less web page,
screenshot it with headless chromium, write it atomically into the public image
tree, and hand back the URL. The web page mounts the same component the profile
viewer uses, so the posted image cannot drift from the site.

Rendered once per outfit and reused. The fingerprint in the filename means a
player who has not changed gear costs nothing after the first render, which is
what makes it affordable to attach to every personal best — and, for the
avatar, what makes it affordable to put on all fifty rows of a leaderboard.
"""
from __future__ import annotations

import os
from typing import Optional

from services.player_model import ensure_public_dir, model_exists

# Keep in sync with WIDTH/HEIGHT in the /model-image page.
MODEL_IMAGE_WIDTH = 400
MODEL_IMAGE_HEIGHT = 600
# Keep in sync with AVATAR_SIZE there. Rendered at 2x, so a 256px source
# covers every tile the site draws, down to the 24px rows.
AVATAR_IMAGE_SIZE = 128

IMAGE_ROOT = "/store/droptracker/disc/static/assets/img/models"
PUBLIC_BASE = "https://www.droptracker.io/img/models"

# SwiftShader is CPU rasterisation, so a render is seconds rather than
# milliseconds. Well beyond that means something is wrong, not slow.
_RENDER_TIMEOUT_SECONDS = 45.0


def _ready_js(framing: str) -> str:
    """The renderer's readiness probe for one framing.

    The renderer draws nothing until the model has loaded and a frame has been
    presented; the screenshot service's built-in probe only knows about images,
    so the page has to say when it is done.

    The framing is part of that, not just the flag: a web server still running a
    build that predates avatars answers `?avatar=1` with a full-body render, and
    nothing downstream would notice it had stored the wrong picture under the
    avatar's name. Never ready is the right failure — the file is not written,
    and the next run tries again against a newer build.
    """
    return f"window.__modelReady === true && window.__modelFraming === '{framing}'"


def image_path(player_id: int, fingerprint: str) -> str:
    return os.path.join(IMAGE_ROOT, str(int(player_id)), f"{fingerprint}.png")


def image_url(player_id: int, fingerprint: str) -> str:
    return f"{PUBLIC_BASE}/{int(player_id)}/{fingerprint}.png"


def image_exists(player_id: int, fingerprint: str) -> bool:
    return os.path.exists(image_path(player_id, fingerprint))


def avatar_path(player_id: int, fingerprint: str) -> str:
    return os.path.join(IMAGE_ROOT, str(int(player_id)), f"{fingerprint}-avatar.png")


def avatar_url(player_id: int, fingerprint: str) -> str:
    return f"{PUBLIC_BASE}/{int(player_id)}/{fingerprint}-avatar.png"


def avatar_exists(player_id: int, fingerprint: str) -> bool:
    return os.path.exists(avatar_path(player_id, fingerprint))


def _page_url(player_id: int, fingerprint: str, *, with_pet: bool = False,
              avatar: bool = False) -> str:
    base = os.getenv("WEB_BASE_URL", "http://127.0.0.1:31380")
    token = os.getenv("BOARD_IMAGE_TOKEN", "")
    url = f"{base}/model-image/{int(player_id)}/{fingerprint}?k={token}"
    if avatar:
        url += "&avatar=1"
    elif with_pet:
        # A bust crop cannot show a pet standing beside the player.
        url += "&pet=1"
    return url


def _write_png(path: str, png: bytes, player_id: int) -> bool:
    """Atomically publish a rendered PNG. Returns whether it landed."""
    ensure_public_dir(os.path.dirname(path))
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(png)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
    except OSError as exc:
        print(f"Could not write {os.path.basename(path)} for player {player_id}: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


async def _render(player_id: int, fingerprint: str, *, path: str, url: str,
                  page_url: str, width: int, height: int, framing: str,
                  label: str) -> Optional[str]:
    """Screenshot one variant of the render page and publish it.

    Returns None rather than raising: these pictures decorate a notification or
    a leaderboard row, and neither may be delayed or lost because one could not
    be drawn.
    """
    if not fingerprint or not model_exists(player_id, fingerprint):
        return None

    if os.path.exists(path):
        return url

    from services.page_screenshot import screenshot_url

    try:
        png = await screenshot_url(
            page_url,
            width=width,
            viewport_height=height,
            scale=2.0,
            timeout=_RENDER_TIMEOUT_SECONDS,
            ready_js=_ready_js(framing),
            # These are cached under the outfit's fingerprint and reused
            # forever, so a blank capture is not a bad frame — it is a bad
            # picture served for as long as the player keeps that gear.
            require_ready=True,
            # A Discord client can be light or dark, and so can the site; a
            # baked-in background rectangle looks broken on whichever one we
            # did not assume.
            transparent=True,
        )
    except Exception as exc:
        print(f"Could not render {label} for player {player_id}: {exc}")
        return None

    return url if _write_png(path, png, player_id) else None


async def render_gear_image(player_id: int, fingerprint: str) -> Optional[str]:
    """Renders and stores the full-body character still, returning its URL."""
    return await _render(
        player_id, fingerprint,
        path=image_path(player_id, fingerprint),
        url=image_url(player_id, fingerprint),
        page_url=_page_url(
            player_id, fingerprint,
            with_pet=model_exists(player_id, fingerprint, pet=True),
        ),
        width=MODEL_IMAGE_WIDTH,
        height=MODEL_IMAGE_HEIGHT,
        framing="full",
        label="gear image",
    )


async def render_avatar_image(player_id: int, fingerprint: str) -> Optional[str]:
    """Renders and stores the square avatar crop, returning its URL."""
    return await _render(
        player_id, fingerprint,
        path=avatar_path(player_id, fingerprint),
        url=avatar_url(player_id, fingerprint),
        page_url=_page_url(player_id, fingerprint, avatar=True),
        width=AVATAR_IMAGE_SIZE,
        height=AVATAR_IMAGE_SIZE,
        framing="bust",
        label="avatar",
    )


def gear_image_for_player(player_id: int) -> Optional[str]:
    """URL of the player's current gear image if one is already rendered.

    Deliberately does no rendering: this is called on the notification path,
    which must not block on a multi-second screenshot. Rendering happens when a
    model is uploaded, so by the time a personal best arrives the image is
    usually already there.

    The avatar has no equivalent here on purpose — every caller wants a whole
    list of them at once, which is ``web_api.common.player_avatars``.
    """
    from db.models import PlayerState, Session

    session = Session()
    try:
        state = (
            session.query(PlayerState)
            .filter(PlayerState.player_id == player_id)
            .first()
        )
        if state is None or not state.model_fingerprint:
            return None
        fingerprint = state.model_fingerprint
    finally:
        session.close()

    if not image_exists(player_id, fingerprint):
        return None
    return image_url(player_id, fingerprint)
