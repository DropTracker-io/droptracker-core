"""The bot's emoji come from its own application, not from someone's guild.

A guild emoji costs the sending bot ``USE_EXTERNAL_EMOJIS`` in the destination
channel, and where that permission is missing the message does not degrade
gracefully — the reader sees the raw ``<:supporter:1263827303712948304>``.
Application emojis need no permission at all.

Two properties are worth pinning:

* **Nothing hardcodes a ``<:name:id>`` any more.** One f-string is all it takes
  to reintroduce the problem, and it reads fine in review — so the check scans
  the tracked source rather than any particular module.
* **A missing emoji never becomes a missing message.** The seeder is a manual
  step against a live Discord app, and one key (``join``) has no surviving art
  at all, so the fallback path is the normal path, not an edge case.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from utils import app_emojis
from utils.app_emojis import (
    MAP_PATH,
    PROFILE_TOKENS,
    SPECS,
    emoji,
    emoji_names,
    load_map,
    validate_specs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``<:name:id>`` / ``<a:name:id>`` written out longhand in source.
_LITERAL = re.compile(r"<a?:[A-Za-z0-9_~]+:\d{15,25}>")

#: Files allowed to name one: the registry and the seeder both quote the old
#: guild emoji in prose to explain what was migrated away from.
_LITERAL_ALLOWED = {"utils/app_emojis.py", "scripts/seed_app_emojis.py"}


@pytest.fixture
def restore_profile():
    """Profile is process-global state; put it back however the test ends."""
    before = app_emojis.current_profile()
    yield
    app_emojis.use_profile(before)


def _tracked_python_files():
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


class TestNoHardcodedEmoji:
    def test_no_module_writes_a_custom_emoji_by_id(self):
        hits = []
        for relative in _tracked_python_files():
            if relative in _LITERAL_ALLOWED or relative.startswith("tests/"):
                continue
            try:
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for literal in _LITERAL.findall(line):
                    hits.append(f"{relative}:{number}: {literal}")
        assert not hits, (
            "These embed a guild emoji id directly, which needs "
            "USE_EXTERNAL_EMOJIS wherever the bot posts it. Add a key to "
            "utils/app_emojis.SPECS and call emoji()/partial_emoji():\n  "
            + "\n  ".join(hits)
        )

    def test_no_module_builds_a_partialemoji_by_id(self):
        # The button/select equivalent of the same mistake, which the
        # <:name:id> scan above cannot see.
        hits = []
        for relative in _tracked_python_files():
            if relative in _LITERAL_ALLOWED or relative.startswith("tests/"):
                continue
            try:
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"PartialEmoji\(\s*name=", line):
                    hits.append(f"{relative}:{number}: {line.strip()}")
        assert not hits, (
            "Use utils.app_emojis.partial_emoji() so the id follows the "
            "running application:\n  " + "\n  ".join(hits)
        )


class TestRegistry:
    def test_specs_are_uploadable_as_written(self):
        assert validate_specs() == []

    def test_key_matches_the_emoji_name(self):
        # The seeder reconciles the app by name and the map by key. If the two
        # ever diverge, a rerun uploads a duplicate instead of reusing.
        assert emoji_names() == {key: key for key in SPECS}

    def test_every_key_has_a_unicode_fallback(self):
        for key, spec in SPECS.items():
            assert spec.fallback, key
            assert not spec.fallback.startswith("<"), f"{key}: fallback is a custom emoji"

    def test_profiles_name_a_token_variable_each(self):
        # The seeder resolves an app from PROFILE_TOKENS[profile]; a profile
        # the bots declare but the seeder cannot reach is unseedable.
        for profile, variable in PROFILE_TOKENS.items():
            assert variable.endswith("TOKEN"), (profile, variable)
        assert app_emojis.DEFAULT_PROFILE in PROFILE_TOKENS


class TestLookup:
    def test_unseeded_profile_falls_back_to_unicode(self, restore_profile):
        app_emojis.use_profile("no-such-app")
        for key, spec in SPECS.items():
            assert emoji(key) == spec.fallback

    def test_unknown_key_is_empty_rather_than_an_exception(self):
        # Called from message-building code — a KeyError here would drop a
        # whole notification, which is far worse than a missing glyph.
        assert emoji("not_a_real_key") == ""

    def test_a_seeded_profile_resolves_to_a_custom_emoji(self, restore_profile):
        seeded = load_map().get("core", {})
        if not seeded:
            pytest.skip("static/app_emojis.json has no core profile yet")
        app_emojis.use_profile("core")
        for key in seeded:
            assert emoji(key) == seeded[key]
            assert _LITERAL.fullmatch(emoji(key)), key

    def test_profile_switch_changes_what_resolves(self, restore_profile):
        mapping = load_map()
        shared = set(mapping.get("core", {})) & set(mapping.get("hof", {}))
        if not shared:
            pytest.skip("no key is seeded on both profiles")
        for key in shared:
            app_emojis.use_profile("core")
            core = emoji(key)
            app_emojis.use_profile("hof")
            assert emoji(key) != core, (
                f"{key} resolves to the same id on both apps — an app emoji "
                "only renders for the application that owns it"
            )


class TestMapFile:
    def test_map_is_keyed_by_profile_then_key(self):
        # A flat {key: ref} map (the rank_emojis shape) would silently serve
        # the core app's ids to the Hall of Fame bot.
        if not Path(MAP_PATH).exists():
            pytest.skip("not seeded on this checkout")
        with open(MAP_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data, "seeded map is empty"
        for profile, entries in data.items():
            assert isinstance(entries, dict), profile
            for key, reference in entries.items():
                assert key in SPECS, f"{profile}.{key} is not in SPECS"
                assert _LITERAL.fullmatch(reference), f"{profile}.{key} = {reference!r}"

    def test_animated_keys_are_mapped_as_animated(self):
        if not Path(MAP_PATH).exists():
            pytest.skip("not seeded on this checkout")
        with open(MAP_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        for profile, entries in data.items():
            for key, reference in entries.items():
                assert reference.startswith("<a:") == SPECS[key].animated, (
                    f"{profile}.{key} is {'animated' if SPECS[key].animated else 'static'} "
                    f"in SPECS but mapped as {reference}"
                )
