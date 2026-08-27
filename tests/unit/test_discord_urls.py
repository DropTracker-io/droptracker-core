"""A group's Discord link must never be a credential.

One production group had a webhook URL stored as its invite, and three
surfaces were publishing it: the group profile, the intake API's group
payload, and the mirror into the forum database. A webhook URL carries its own
token, so publishing one grants write access to that clan's Discord to anyone
who reads the page.

The rule is deliberately a narrow blocklist, not an allowlist of "real" invite
shapes — the field legitimately holds bare ``discord.gg/x``, mixed case, and
custom redirects on a clan's own domain, and rejecting those would break real
groups to catch a problem they do not have.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "utils" / "discord_urls.py"
_spec = importlib.util.spec_from_file_location("_real_discord_urls", _PATH)
durls = importlib.util.module_from_spec(_spec)
sys.modules["_real_discord_urls"] = durls
_spec.loader.exec_module(durls)


class TestCredentialsAreRefused:
    def test_a_webhook_url_is_not_publishable(self):
        url = "https://discord.com/api/webhooks/1509134743151837266/AbCdEf-token_123"
        assert durls.is_discord_credential_url(url)
        assert durls.public_discord_url(url) is None

    def test_every_host_and_scheme_variant(self):
        for url in (
            "https://discordapp.com/api/webhooks/123/tok",
            "http://discord.com/api/webhooks/123/tok",
            "https://ptb.discord.com/api/webhooks/123/tok",
            "https://DISCORD.COM/API/WEBHOOKS/123/tok",
            # A bare relative form, in case one is ever stored without a host.
            "/api/webhooks/123/tok",
        ):
            assert durls.public_discord_url(url) is None, url

    def test_any_discord_api_url_is_refused_not_just_webhooks(self):
        # The API surface is where tokens live; none of it belongs on a
        # group page, so the guard is not webhook-specific.
        assert durls.public_discord_url("https://discord.com/api/v10/users/@me") is None


class TestRealInvitesStillWork:
    def test_the_shapes_groups_actually_use(self):
        # Every one of these is in production today. A stricter allowlist
        # would have silently blanked them.
        for url in (
            "https://discord.gg/droptracker",
            "https://discord.com/invite/abc123",
            "https://discordapp.com/invite/abc123",
            "discord.gg/Galvanize",                      # no scheme
            "www.discord.gg/5Z4Nv",                      # no scheme, www
            "https://Discord.gg/TheGarage",              # mixed case
            "https://kovos.lol/discord",                 # custom redirect
            "https://discord.com/channels/1379825993/1", # channel link
        ):
            assert durls.public_discord_url(url) == url.strip(), url

    def test_blank_and_missing_are_none(self):
        for value in (None, "", "   ", 0, [], {}):
            assert durls.public_discord_url(value) is None

    def test_surrounding_whitespace_is_trimmed(self):
        assert durls.public_discord_url("  https://discord.gg/x  ") == "https://discord.gg/x"


class TestConfigWriteIsRejected:
    def test_pasting_a_webhook_into_the_invite_field_raises(self):
        from web_api.config_registry import ConfigValidationError, coerce_to_storage

        try:
            coerce_to_storage(
                "discord_url",
                "https://discord.com/api/webhooks/123/tok",
            )
        except ConfigValidationError as exc:
            # The message has to name the confusion, because the person doing
            # this has mixed up two settings, not typed a malformed URL.
            assert "webhook" in str(exc).lower()
        else:
            raise AssertionError("a webhook URL was accepted as a public invite")

    def test_a_real_invite_is_still_accepted(self):
        from web_api.config_registry import coerce_to_storage

        assert coerce_to_storage("discord_url", "https://discord.gg/x") == "https://discord.gg/x"
