"""Unit tests for services/suggestion_naming.py (forum thread names)."""
import unittest

from services.suggestion_naming import (
    THREAD_NAME_MAX,
    fallback_note,
    is_numbered,
    strip_number_prefix,
    thread_name,
)

BULB = "\N{ELECTRIC LIGHT BULB}"
BUG = "\N{BUG}"


class TestThreadName(unittest.TestCase):
    def test_suggestion_format(self):
        self.assertEqual(thread_name("suggestion", 123, "Dark theme"), f"{BULB} #123 · Dark theme")

    def test_bug_format(self):
        self.assertEqual(thread_name("bug", 9, "Lootboard stuck"), f"{BUG} #9 · Lootboard stuck")

    def test_capped_at_100_and_keeps_prefix(self):
        name = thread_name("bug", 12345, "x" * 300)
        self.assertEqual(len(name), THREAD_NAME_MAX)
        self.assertTrue(name.startswith(f"{BUG} #12345 · "))

    def test_cap_does_not_leave_trailing_space(self):
        prefix = f"{BULB} #1 · "
        title = "a" * (THREAD_NAME_MAX - len(prefix) - 1) + " " + "b" * 20
        self.assertFalse(thread_name("suggestion", 1, title).endswith(" "))

    def test_legacy_emoji_title_is_not_doubled(self):
        self.assertEqual(thread_name("suggestion", 4, f"{BULB} Dark theme"), f"{BULB} #4 · Dark theme")

    def test_existing_number_is_replaced_not_stacked(self):
        self.assertEqual(thread_name("bug", 8, f"{BUG} #8 · Crash"), f"{BUG} #8 · Crash")

    def test_empty_title_falls_back(self):
        self.assertEqual(thread_name("bug", 3, ""), f"{BUG} #3 · Untitled")

    def test_unknown_kind_has_no_emoji(self):
        self.assertEqual(thread_name("other", 5, "Hi"), "#5 · Hi")


class TestStripAndDetect(unittest.TestCase):
    def test_strips_our_prefix(self):
        self.assertEqual(strip_number_prefix(f"{BULB} #123 · Dark theme"), "Dark theme")
        self.assertEqual(strip_number_prefix("#7 · Bare"), "Bare")

    def test_leaves_user_text_alone(self):
        self.assertEqual(strip_number_prefix("#5 broken"), "#5 broken")
        self.assertEqual(strip_number_prefix("Crash on #5 · boss"), "Crash on #5 · boss")
        self.assertEqual(strip_number_prefix(f"{BUG} crash"), f"{BUG} crash")

    def test_is_numbered_matches_only_own_id(self):
        name = f"{BULB} #12 · Title"
        self.assertTrue(is_numbered(name, 12))
        self.assertFalse(is_numbered(name, 1))
        self.assertFalse(is_numbered(name, 123))
        self.assertFalse(is_numbered(f"{BULB} Title", 12))

    def test_is_numbered_roundtrip(self):
        self.assertTrue(is_numbered(thread_name("bug", 77, "x" * 200), 77))

    def test_fallback_note(self):
        self.assertEqual(fallback_note("bug", 7), "-# Tracked as bug report #7")
        self.assertEqual(fallback_note("suggestion", 7), "-# Tracked as suggestion #7")
