"""Torso-up avatar crops derived from a player's character render.

``services/gear_image.py`` produces one 800x1200 transparent PNG per outfit —
a full-body shot, framed for a Discord embed. That picture is the wrong thing
to put in a leaderboard row twice over: it is ~80 KB (a hundred-row board would
pull 8 MB of avatars), and at 24 px a full body is an unreadable smudge of
mostly legs.

So the avatar is a *derived* artifact: a square, head-and-torso crop, cached on
disk beside the render as ``{fingerprint}-avatar.png``. Deriving once and
caching is what makes it affordable — the crop is ~15 KB against the render's
~80 KB, and a player who has not changed gear costs nothing after the first
request.

Why a real cached crop rather than a CSS transform on the full render: CSS would
still ship the 80 KB body to every row and still decode 800x1200 per avatar. The
whole point is to not send that.


How the crop is located
-----------------------
The naive approach — take a fixed slice off the top of the image — breaks on
real data, and the reason is worth recording. The render's *bounding box* is not
the character: a spear, a godsword or a banner held overhead extends the alpha
bounds well above the head, and a fixed offset from the top of the bbox then
frames the weapon instead of the face. Measured across a 55-render sample, the
bbox top varies by ~200 px for exactly this reason.

The **feet** are, by contrast, extremely stable — the bottom of the alpha
bounds, stdev 7 px out of 1200 (0.6%) — because the camera is fixed, every
character is the same rig in different clothes, and nothing a player can wear or
hold extends below the ground plane. Everything here is measured relative to
them, so camera drift or a re-render at another size self-corrects.

Three quantities are then derived, each guarded against a different way a held
item can impersonate the player:

* **the body axis** — mass median of the strip just above the feet, which the
  legs dominate. Immune to weapons, but it is not where the *torso* is: measured
  over 145 renders, the head sits a systematic 8.4 px (of 128) to one side of
  the leg centre, because the idle stance is not symmetric.
* **the horizontal centre** — mass median over the region actually being
  cropped, which centres the face rather than the stance, *leashed* to the body
  axis so a tower shield cannot capture it outright.
* **the crown** — found by scanning down for the first contiguous run across the
  centre line that is at least head-wide. Width is the discriminator, and it is
  the whole trick: a godsword held upright passes straight through the centre
  line, so any "topmost pixel above the torso" test frames the blade and pushes
  the face to the bottom of the tile. A blade is ~0.01 of a body across; a skull
  is ~0.06. The scan is confined to the band a head can physically occupy, which
  bounds the damage when a weapon does slip through — the figure sits low, but
  its head is still in frame.

Anchoring vertically on the detected crown rather than on a fixed offset from
the feet is what lets the avatar be drawn with no background behind it. Headgear
is real height that varies by ~0.16 of a body height (a wizard hat against a
bare head), so a fixed offset left the figure sitting low by a different amount
for every player — measured at a 20 px gap above the head against 0 below. A
solid tile hid that; transparency does not.

Renders that fail the sanity check — a partial or broken screenshot, of which
there are some on disk — yield no crop at all, which surfaces as the letter
placeholder. A missing avatar is the designed default (most players have no
model), so degrading into it costs nothing.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from services.player_model import ensure_public_dir

# Output size. 128 px covers every avatar slot the site draws (the largest is
# the 56 px profile tile, doubled for retina) without a per-surface variant.
AVATAR_SIZE = 128

# The character's height as a fraction of the render's height. From the fixed
# camera in the /model-image page: measured mean 629 px of 1200 across 55
# renders. Expressed as a ratio so a future re-render at another size still
# works without touching this.
_BODY_FRACTION = 0.525

# Where a bare head's crown sits above the feet, in body heights. Only a
# fallback: the crown is detected per image, and this stands in when detection
# fails, so it is the *typical* head rather than the tallest.
_NOMINAL_HEAD = 1.04

# Gap above the crown, as a fraction of the crop's side. Small and deliberate:
# with no background fill the figure is the whole picture, and dead space above
# the head reads as the avatar sitting low in its frame rather than as framing.
_HEADROOM = 0.055

# Side of the square crop, in body heights. 0.50 puts the bottom edge just under
# the chest, which is the "torso-up, no legs" framing this is for, and fills the
# tile better than a looser crop once there is no background behind it.
_SIDE = 0.50

# The strip above the feet used to find the horizontal centre, in body heights.
_BOOT_BAND = 0.11

# Narrowest contiguous run, in body heights, that counts as a head rather than a
# held weapon. Measured: a blade is ~0.01 of a body across and a skull ~0.06, so
# anything in between separates them; erring low keeps the detector working on
# renders where only the crown is visible above a cape.
_MIN_HEAD_WIDTH = 0.045

# Alpha at or below this is background, not character. Renders are composited
# against transparency, so edge pixels feather.
_ALPHA_FLOOR = 16


# Cache of player_id -> (fingerprint or None, expiry). The site asks for an
# avatar per row, and the overwhelming majority of those rows are players with
# no model at all, so the negative answer has to be as cheap as the positive
# one: without this, a hundred-row leaderboard is a hundred SELECTs.
#
# Short TTL because it is the freshness bound on "I just uploaded a new outfit,
# why is my avatar stale" — a few minutes is imperceptible, and the edge holds
# the picture itself for longer than this anyway.
_FINGERPRINT_TTL_SECONDS = 300
_MAX_FINGERPRINT_ENTRIES = 50_000
_fingerprint_cache: dict[int, tuple[Optional[str], float]] = {}


def current_fingerprint(player_id: int) -> Optional[str]:
    """The outfit a player's avatar should show, or None if they have no model.

    Prefers the pinned outfit — that is the one the player explicitly chose via
    the plugin's "Send Player Model" button, and a pin is a promise that their
    profile shows *that*, not whatever they happened to log out in.
    """
    cached = _fingerprint_cache.get(player_id)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]

    fingerprint = None
    try:
        from db.models import PlayerState, Session

        session = Session()
        try:
            state = (
                session.query(PlayerState)
                .filter(PlayerState.player_id == player_id)
                .first()
            )
            if state is not None:
                fingerprint = state.pinned_model_fingerprint or state.model_fingerprint
        finally:
            session.close()
    except Exception as exc:
        # A DB blip must not turn every avatar on the page into a broken image;
        # answering "no model" degrades to the letter tile, which is the normal
        # appearance for most players anyway. Not cached, so it self-heals.
        print(f"Could not resolve avatar fingerprint for player {player_id}: {exc}")
        return None

    if len(_fingerprint_cache) >= _MAX_FINGERPRINT_ENTRIES:
        _fingerprint_cache.clear()
    _fingerprint_cache[player_id] = (
        fingerprint,
        time.monotonic() + _FINGERPRINT_TTL_SECONDS,
    )
    return fingerprint


def avatar_path(player_id: int, fingerprint: str) -> str:
    from services.gear_image import IMAGE_ROOT

    return os.path.join(IMAGE_ROOT, str(int(player_id)), f"{fingerprint}-avatar.png")


def avatar_url(player_id: int, fingerprint: str) -> str:
    from services.gear_image import PUBLIC_BASE

    return f"{PUBLIC_BASE}/{int(player_id)}/{fingerprint}-avatar.png"


def avatar_exists(player_id: int, fingerprint: str) -> bool:
    return os.path.exists(avatar_path(player_id, fingerprint))


def _find_crown(solid, body: float, feet: int, centre_x: float,
                width: int) -> Optional[float]:
    """Row of the top of the player's head, or None if it cannot be found.

    Walks down the band where a head can possibly be and returns the first row
    whose contiguous run across the body's centre line is at least head-wide.
    Only the plausible band is scanned, so this is a few hundred short row
    operations rather than a pass over the image.

    Returning None is a normal outcome (~12% of renders — hair merged into a
    cape, an obscured head) and the caller falls back to the nominal head
    position, which is sound because the camera is fixed.
    """
    import numpy as np

    minimum = max(3, int(body * _MIN_HEAD_WIDTH))
    reach = body * 0.10
    x = int(centre_x)
    start = max(0, int(feet - body * 1.20))
    stop = max(1, int(feet - body * 0.95))

    for y in range(start, stop):
        row = solid[y]
        lit = np.flatnonzero(row)
        if lit.size == 0:
            continue
        # Nearest lit pixel to the body's centre line; anything further away
        # than an arm's reach belongs to something the player is holding.
        nearest = int(lit[np.argmin(np.abs(lit - x))])
        if abs(nearest - x) > reach:
            continue
        left = nearest
        while left > 0 and row[left - 1]:
            left -= 1
        right = nearest
        while right < width - 1 and row[right + 1]:
            right += 1
        if (right - left + 1) >= minimum:
            return float(y)
    return None


def _crop_box(alpha) -> Optional[Tuple[int, int, int, int]]:
    """Locates the torso-up square in an alpha channel, or None if implausible.

    Split out from the file handling so the geometry can be unit-tested against
    synthetic figures without writing any PNGs.
    """
    import numpy as np

    a = np.asarray(alpha)
    height, width = a.shape
    solid = a > _ALPHA_FLOOR
    rows = np.flatnonzero(solid.any(axis=1))
    cols = np.flatnonzero(solid.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None

    top, bottom = int(rows[0]), int(rows[-1])
    feet = bottom

    # Sanity: a real render is a standing figure occupying most of the frame and
    # resting near the bottom. Anything else is a broken screenshot — there are
    # such files on disk (one measured at 3.9% of the frame height) and framing
    # one would produce a confident-looking picture of nothing.
    if (bottom - top) < height * 0.20 or feet < height * 0.5:
        return None

    body = height * _BODY_FRACTION

    # Horizontal centre, taken over the boot band only — legs are always under
    # the body, whereas anything at head height may be a held weapon.
    band_top = max(0, feet - int(body * _BOOT_BAND))
    band = a[band_top:feet + 1].astype("float64")
    band[band <= _ALPHA_FLOOR] = 0.0
    column_mass = band.sum(axis=0)
    if column_mass.sum() <= 0:
        return None

    # Locate the legs with the mass *median*, then average only its
    # neighbourhood. Two steps, each fixing a different failure:
    #
    # A plain mean across the band is not robust to a second body in the frame —
    # a pet standing at the player's feet is a solid blob down here and drags
    # the mean sideways (measured at 70 px on a synthetic pet, enough to put a
    # shoulder where the face should be). The median barely moves, because the
    # pet is a minority of the mass rather than a distant outlier.
    #
    # But the median alone lands off-centre when mass is lopsided, so it is used
    # only to *find* the legs; the returned centre is a centroid over a window
    # around it. The window is what actually excludes the pet, and it is wide
    # enough to average across both legs rather than latching onto one.
    cumulative = np.cumsum(column_mass)
    median = int(np.searchsorted(cumulative, cumulative[-1] / 2.0))
    half = max(1, int(body * 0.10))
    lo, hi = max(0, median - half), min(width, median + half + 1)
    window = column_mass[lo:hi]
    axis_x = float((window * np.arange(lo, hi)).sum() / window.sum())

    # The legs give the body's *axis*, but not the centre of what this crop
    # actually shows. Measured over 145 renders, the head sits a systematic
    # 8.4 px (of 128) to one side of the leg centre — the idle stance is not
    # symmetric — so centring on the axis alone leaves every face visibly
    # off-centre. Re-take the centre over the region being cropped, where the
    # torso dominates the mass.
    torso = a[max(0, int(feet - body * 1.20)):max(1, int(feet - body * 0.53))]
    torso = torso.astype("float64")
    torso[torso <= _ALPHA_FLOOR] = 0.0
    torso_mass = torso.sum(axis=0)
    if torso_mass.sum() <= 0:
        return None
    torso_cum = np.cumsum(torso_mass)
    centre_x = float(np.searchsorted(torso_cum, torso_cum[-1] / 2.0))

    # ...but leashed to the body axis. A tower shield or a banner is enough mass
    # up here to capture the median outright, and an avatar centred on someone's
    # shield with their head in the corner is worse than one a few pixels off.
    # The legs cannot be captured that way, so they get the final say on how far
    # this is allowed to travel.
    leash = body * 0.05
    centre_x = min(max(centre_x, axis_x - leash), axis_x + leash)

    # Vertical anchor: find the actual crown, rather than measuring down from
    # the feet. Headgear is real height that varies by ~0.16 of a body height
    # (a wizard hat against a bare head), so a fixed offset leaves the figure
    # sitting low in the tile by a different amount for every player — measured
    # at a 20 px top gap against 0 px at the bottom, which a solid tile hid and
    # a transparent one would not.
    #
    # A head is found by how WIDE it is, not by where the topmost pixel is.
    # That distinction is the whole detector: a godsword or staff held upright
    # passes straight through the body's centre line, so any test based on "the
    # first thing above the torso" frames the blade and pushes the player's face
    # to the bottom of the tile (observed on 6 of 20 sampled renders). A blade
    # is a few pixels across and a skull is tens, so requiring a contiguous run
    # of head-like width tells them apart directly.
    nominal = feet - body * _NOMINAL_HEAD
    crown = _find_crown(solid, body, feet, centre_x, width)
    if crown is None:
        crown = nominal

    # Rounded to an integer side and added to a rounded origin, rather than
    # rounding all four edges independently — that can land a 314x315 box, which
    # the resize to a square then stretches by a third of a percent. Small, but
    # it is a distortion applied to faces for no reason.
    side = int(round(body * _SIDE))
    left = int(round(centre_x - side / 2.0))
    box_top = int(round(crown - side * _HEADROOM))
    return (left, box_top, left + side, box_top + side)


def build_avatar(source_path: str, size: int = AVATAR_SIZE):
    """Returns the cropped avatar for a render, or None if it is not usable."""
    from PIL import Image

    with Image.open(source_path) as img:
        img = img.convert("RGBA")
        box = _crop_box(img.split()[-1])
        if box is None:
            return None
        # A crop box may run past the edge; Pillow pads with transparency,
        # which is exactly right here — the render already has none there.
        return img.crop(box).resize((size, size), Image.LANCZOS)


def ensure_avatar(player_id: int, fingerprint: str) -> Optional[str]:
    """Derives and caches the avatar crop, returning its path.

    Idempotent and cheap on a hit (one ``stat``). Returns None when there is no
    render to crop or the render is not a usable figure — both of which the
    caller answers with the letter placeholder.
    """
    from services.gear_image import image_path

    target = avatar_path(player_id, fingerprint)
    if os.path.exists(target):
        return target

    source = image_path(player_id, fingerprint)
    if not os.path.exists(source):
        return None

    try:
        avatar = build_avatar(source)
    except Exception as exc:
        print(f"Could not build avatar for player {player_id}: {exc}")
        return None
    if avatar is None:
        return None

    ensure_public_dir(os.path.dirname(target))
    # Written under a temporary name and renamed, so a concurrent reader never
    # sees a half-written PNG — the same contract store_model gives for models.
    tmp = f"{target}.tmp"
    try:
        avatar.save(tmp, "PNG", optimize=True)
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o666)
        except OSError:
            pass
    except OSError as exc:
        # The image tree is written by `user` and read by `debian`; a
        # PermissionError here is the failure mode that has bitten this repo
        # before, so it is logged rather than silently swallowed.
        print(f"Could not write avatar for player {player_id}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target
