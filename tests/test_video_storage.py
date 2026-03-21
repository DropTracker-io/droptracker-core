import unittest
import types
import sys

fake_b2_storage = types.ModuleType("utils.b2_storage")
fake_b2_storage.get_public_video_url = lambda object_key: f"https://example.invalid/{object_key}"


async def _noop_async(*args, **kwargs):
    return True


fake_b2_storage.download_object = _noop_async
fake_b2_storage.upload_object = _noop_async
fake_b2_storage.delete_object = _noop_async
fake_b2_storage.object_exists = _noop_async
sys.modules["utils.b2_storage"] = fake_b2_storage

from utils.video_storage import (
    build_raw_key,
    derive_final_key,
    normalize_backend,
    resolve_internal_path,
    VIDEO_LOCAL_RETENTION_MINUTES,
)


class TestVideoStorage(unittest.TestCase):
    def test_build_raw_key_and_derive_final_key(self):
        raw_key = build_raw_key(player_id=12345, video_uuid="abc-uuid", fps=20)
        self.assertEqual(raw_key, "raw/12345/abc-uuid_fps20.mjpeg")
        self.assertEqual(derive_final_key(raw_key), "videos/12345/abc-uuid.mp4")

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

