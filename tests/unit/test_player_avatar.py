"""Geometry of the torso-up avatar crop.

The interesting cases are not "does Pillow crop" but the two things that made a
naive crop wrong on real data: held weapons that extend the alpha bounds above
the head, and broken renders that are not a figure at all. The fixtures below
are synthetic silhouettes rather than real screenshots, so each of those can be
constructed in isolation.
"""
from PIL import Image

from services.player_avatar import _BODY_FRACTION, _crop_box, build_avatar

WIDTH, HEIGHT = 800, 1200
# Where the fixed camera puts the character; mirrors the measured production
# render (feet at y~930, body ~63% of the frame).
FEET = 930
BODY = int(HEIGHT * _BODY_FRACTION)
HEAD = FEET - BODY


def figure(*, centre=400, extras=()):
    """A standing silhouette, plus optional extra rectangles (weapons, pets).

    Returns the alpha channel, which is all ``_crop_box`` reads.
    """
    img = Image.new("L", (WIDTH, HEIGHT), 0)
    body = Image.new("L", (120, BODY), 255)
    img.paste(body, (centre - 60, HEAD))
    for box in extras:
        left, top, right, bottom = box
        img.paste(Image.new("L", (right - left, bottom - top), 255), (left, top))
    return img


def centre_of(box):
    return (box[0] + box[2]) / 2


class TestCropBox:
    def test_frames_the_torso_and_excludes_the_legs(self):
        box = _crop_box(figure())
        assert box is not None
        left, top, right, bottom = box
        # Square, so it drops into a round or rounded-square slot undistorted.
        assert right - left == bottom - top
        # Top edge sits above the head, bottom edge well above the feet.
        assert top < HEAD
        assert bottom < FEET
        # The excluded strip is the legs: over a third of the body height.
        assert (FEET - bottom) > BODY * 0.35

    def test_centres_on_the_character(self):
        assert abs(centre_of(_crop_box(figure(centre=400))) - 400) < 15

    def test_follows_a_character_standing_off_centre(self):
        assert abs(centre_of(_crop_box(figure(centre=300))) - 300) < 15

    def test_a_weapon_held_overhead_does_not_pull_the_crop_up(self):
        """The regression this module exists for.

        A spear reaching above the head extends the alpha bounding box, and a
        crop anchored to that box frames the weapon instead of the face.
        """
        spear = (395, 100, 405, FEET)
        plain = _crop_box(figure())
        armed = _crop_box(figure(extras=(spear,)))
        assert armed is not None
        # Same framing despite the bounding box now starting 400px higher.
        assert abs(armed[1] - plain[1]) < 20
        assert armed[1] < HEAD

    def test_a_weapon_held_to_one_side_does_not_pull_the_crop_sideways(self):
        # A wide banner off the character's left: mass well outside the body,
        # but not in the boot band the centroid is taken over.
        banner = (620, 200, 760, 700)
        assert abs(centre_of(_crop_box(figure(extras=(banner,)))) - 400) < 25

    def test_a_staff_resting_on_the_ground_barely_moves_the_centre(self):
        """A bounding-box centre would swing here; a mass-weighted one does not.

        The staff butt sits inside the boot band, so it is seen — but it is a
        sliver, and weighting by alpha rather than extent keeps it negligible.
        """
        staff = (560, 300, 572, FEET)
        assert abs(centre_of(_crop_box(figure(extras=(staff,)))) - 400) < 25

    def test_a_pet_at_the_feet_does_not_become_the_subject(self):
        pet = (520, FEET - 90, 610, FEET)
        assert abs(centre_of(_crop_box(figure(extras=(pet,)))) - 400) < 45


class TestRejectsWhatIsNotAFigure:
    def test_rejects_an_empty_render(self):
        assert _crop_box(Image.new("L", (WIDTH, HEIGHT), 0)) is None

    def test_rejects_a_partial_render(self):
        """Broken screenshots exist on disk — one measured at 3.9% of the frame.

        Cropping one yields a confident-looking picture of nothing, so it must
        fail here and fall back to the letter placeholder instead.
        """
        sliver = Image.new("L", (WIDTH, HEIGHT), 0)
        sliver.paste(Image.new("L", (500, 47), 255), (84, 577))
        assert _crop_box(sliver) is None

    def test_rejects_a_figure_floating_above_the_ground_plane(self):
        # Nothing the camera frames ends halfway up; this is a broken render.
        floating = Image.new("L", (WIDTH, HEIGHT), 0)
        floating.paste(Image.new("L", (120, 400), 255), (340, 50))
        assert _crop_box(floating) is None


class TestBuildAvatar:
    def test_produces_a_square_image(self, tmp_path):
        source = tmp_path / "render.png"
        Image.merge("RGBA", (figure(),) * 4).save(source)
        avatar = build_avatar(str(source))
        assert avatar is not None
        assert avatar.size == (128, 128)

    def test_returns_none_for_an_unusable_render(self, tmp_path):
        source = tmp_path / "broken.png"
        blank = Image.new("L", (WIDTH, HEIGHT), 0)
        Image.merge("RGBA", (blank,) * 4).save(source)
        assert build_avatar(str(source)) is None
