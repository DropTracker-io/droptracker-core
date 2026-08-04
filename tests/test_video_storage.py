"""Unit tests for the video storage key/path helpers.

``utils.video_storage`` imports the boto3-backed ``utils.b2_storage`` at module
level; ``tests/conftest.py`` already stubs that name in ``sys.modules``, so this
module must NOT install a stub of its own. It used to assign a hand-rolled
``types.ModuleType`` there at import time, which replaced the stub for the whole
session — and because that fake omitted ``generate_presigned_upload_url``, every
later test module that imported ``api`` (``api/routes/video.py`` imports the
name at module level) died during collection. ``tests/`` sorts before
``tests/unit/``, so it took the suite down deterministically.
"""

import unittest

from utils.video_storage import (
    build_raw_key,
    derive_final_key,
    normalize_backend,
    resolve_internal_path,
    VIDEO_LOCAL_RETENTION_MINUTES,
)


class TestVideoStorage(unittest.TestCase):
    def test_build_raw_key_and_derive_final_key(self):
        # The dt_ prefixes are load-bearing: b167e90 moved the video objects out
        # of the bucket root to stop the backup sync's key rules 403ing them.
        raw_key = build_raw_key(player_id=12345, video_uuid="abc-uuid", fps=20)
        self.assertEqual(raw_key, "dt_raw/12345/abc-uuid_fps20.mjpeg")
        self.assertEqual(derive_final_key(raw_key), "dt_videos/12345/abc-uuid.mp4")

    def test_derive_final_key_still_accepts_legacy_raw_prefix(self):
        # Objects uploaded before b167e90 keep the bare "raw/" prefix in the
        # video_uploads rows, and the worker still has to convert them.
        self.assertEqual(
            derive_final_key("raw/12345/abc-uuid_fps20.mjpeg"),
            "dt_videos/12345/abc-uuid.mp4",
        )

    def test_normalize_backend_defaults_to_b2(self):
        self.assertEqual(normalize_backend("b2"), "b2")
        self.assertEqual(normalize_backend("local"), "local")
        self.assertEqual(normalize_backend("unknown-backend"), "b2")
        self.assertEqual(normalize_backend(None), "b2")

    def test_resolve_internal_path_for_local_keys(self):
        raw_path = resolve_internal_path("raw/1/sample_fps20.mjpeg", backend="local")
        final_path = resolve_internal_path("videos/1/sample.mp4", backend="local")
        self.assertTrue(raw_path.endswith("/1/sample_fps20.mjpeg"))
        self.assertTrue(final_path.endswith("/1/sample.mp4"))

    def test_resolve_internal_path_blocks_path_traversal(self):
        with self.assertRaises(ValueError):
            resolve_internal_path("raw/../../etc/passwd", backend="local")

    def test_local_retention_is_capped_to_one_hour(self):
        self.assertGreaterEqual(VIDEO_LOCAL_RETENTION_MINUTES, 1)
        self.assertLessEqual(VIDEO_LOCAL_RETENTION_MINUTES, 60)


if __name__ == "__main__":
    unittest.main()

