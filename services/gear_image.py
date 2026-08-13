"""Server-rendered picture of a player's character, for Discord notifications.

Mirrors :mod:`services.recap_image`: build a URL for a chrome-less web page,
screenshot it with headless chromium, write it atomically into the public image
tree, and hand back the URL. The web page mounts the same component the profile
viewer uses, so the posted image cannot drift from the site.

Rendered once per outfit and reused. The fingerprint in the filename means a
player who has not changed gear costs nothing after the first render, which is
what makes it affordable to attach to every personal best.
"""
from __future__ import annotations

import os
from typing import Optional

from services.player_model import ensure_public_dir, model_exists

# Keep in sync with WIDTH/HEIGHT in the /model-image page.
MODEL_IMAGE_WIDTH = 400
MODEL_IMAGE_HEIGHT = 600

IMAGE_ROOT = "/store/droptracker/disc/static/assets/img/models"
PUBLIC_BASE = "https://www.droptracker.io/img/models"

# The renderer draws nothing until the model has loaded and a frame has been
# presented; the built-in readiness probe only knows about images.
_READY_JS = "window.__modelReady === true"

# SwiftShader is CPU rasterisation, so a render is seconds rather than
# milliseconds. Well beyond that means something is wrong, not slow.
_RENDER_TIMEOUT_SECONDS = 45.0


def image_path(player_id: int, fingerprint: str) -> str:
    return os.path.join(IMAGE_ROOT, str(int(player_id)), f"{fingerprint}.png")


def image_url(player_id: int, fingerprint: str) -> str:
    return f"{PUBLIC_BASE}/{int(player_id)}/{fingerprint}.png"


def image_exists(player_id: int, fingerprint: str) -> bool:
    return os.path.exists(image_path(player_id, fingerprint))


def _page_url(player_id: int, fingerprint: str, *, with_pet: bool) -> str:
    base = os.getenv("WEB_BASE_URL", "http://127.0.0.1:31380")
    token = os.getenv("BOARD_IMAGE_TOKEN", "")
    url = f"{base}/model-image/{int(player_id)}/{fingerprint}?k={token}"
    if with_pet:
        url += "&pet=1"
    return url


async def render_gear_image(player_id: int, fingerprint: str) -> Optional[str]:
    """Renders and stores the character image, returning its public URL.

    Returns None rather than raising: this decorates a notification, and a
    notification must never be delayed or lost because a picture could not be
    drawn.
    """
    if not fingerprint or not model_exists(player_id, fingerprint):
        return None

    if image_exists(player_id, fingerprint):
        return image_url(player_id, fingerprint)

    from services.page_screenshot import screenshot_url

    with_pet = model_exists(player_id, fingerprint, pet=True)
    try:
        png = await screenshot_url(
            _page_url(player_id, fingerprint, with_pet=with_pet),
            width=MODEL_IMAGE_WIDTH,
            viewport_height=MODEL_IMAGE_HEIGHT,
            scale=2.0,
            timeout=_RENDER_TIMEOUT_SECONDS,
            ready_js=_READY_JS,
            # A Discord client can be light or dark; a baked-in background
            # rectangle looks broken on whichever one we did not assume.
            transparent=True,
        )
    except Exception as exc:
        print(f"Could not render gear image for player {player_id}: {exc}")
        return None

    final_path = image_path(player_id, fingerprint)
    ensure_public_dir(os.path.dirname(final_path))
    tmp_path = f"{final_path}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(png)
        os.replace(tmp_path, final_path)
        try:
            os.chmod(final_path, 0o666)
        except OSError:
            pass
    except OSError as exc:
        print(f"Could not write gear image for player {player_id}: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None

    return image_url(player_id, fingerprint)


def gear_image_for_player(player_id: int) -> Optional[str]:
    """URL of the player's current gear image if one is already rendered.

    Deliberately does no rendering: this is called on the notification path,
    which must not block on a multi-second screenshot. Rendering happens when a
    model is uploaded, so by the time a personal best arrives the image is
    usually already there.
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
