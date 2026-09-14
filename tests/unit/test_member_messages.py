"""Members' own death messages — the shared rules in db/member_messages.py.

Validation (what a member may save), the tolerant reader for stored rows, the
send-time sanitizer, which template wins and how it renders, and the storage
helpers' fail-closed behaviour. conftest loads the real module by path under
the stubbed ``db`` package, so these exercise the rule every surface uses.
"""
import json
import random
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

mm = sys.modules["db.member_messages"]


def _issue(messages):
    try:
        mm.normalize_messages(messages)
    except mm.MemberMessageError as exc:
        return str(exc)
    return None


class TestNormalizeMessages:
    def test_trims_drops_blank_rows_and_duplicates(self):
        assert mm.normalize_messages([
            "  {player_name} forgot to pray  ", "", "   ", "{player_name} forgot to pray",
        ]) == ["{player_name} forgot to pray"]

    def test_accepts_a_json_array_string(self):
        assert mm.normalize_messages('["{player_name} planked"]') == ["{player_name} planked"]

    def test_nothing_is_an_empty_list(self):
        assert mm.normalize_messages(None) == []
        assert mm.normalize_messages("") == []
        assert mm.normalize_messages([]) == []
        assert mm.normalize_messages(["", "  "]) == []

    def test_lowercases_placeholder_names(self):
        assert mm.normalize_messages(["{Player_Name} vs {KILLER}"]) == ["{player_name} vs {killer}"]

    def test_shape_errors(self):
        assert _issue("not json") == "Messages must be a list of text lines."
        assert _issue({"a": 1}) == "Messages must be a list of text lines."
        assert _issue(["ok", 5]) == "Messages must be a list of text lines."

    def test_at_most_five_messages(self):
        assert "at most 5 messages" in _issue([f"message {i}" for i in range(6)])
        assert len(mm.normalize_messages([f"message {i}" for i in range(5)])) == 5
        # A huge payload is refused before every entry is examined.
        assert "at most 5 messages" in _issue(["x"] * 1000)

    def test_length_limit(self):
        assert _issue(["x" * 151]) == "Each message can be at most 150 characters."
        assert _issue(["x" * 150]) is None

    @pytest.mark.parametrize("text", [
        "@everyone rip", "@here rip", "<@123> rip", "<@!123> rip", "<@&456> rip",
        "<#789> rip", "<:skull:123456> rip", "<a:dance:123456> rip",
        "</settings:123456> rip", "<t:1700000000:R> rip",
    ])
    def test_no_mentions_or_discord_entities(self, text):
        assert _issue([text]) == (
            "Messages can't mention people, roles or channels, or use custom emoji."
        )

    @pytest.mark.parametrize("text", [
        "see https://example.com", "http://x.y", "www.example.org rip",
        "join discord.gg/abcdef", "discord.com/invite/abc", "free gp at scam.xyz",
        "[click me](https://example.com)",
    ])
    def test_no_links(self, text):
        assert _issue([text]) == "Messages can't contain links."

    @pytest.mark.parametrize("text", [
        "# {player_name} died", "## big", "-# small", "> quoted", ">>> quoted",
    ])
    def test_no_headings_or_quotes(self, text):
        assert _issue([text]) == "Messages can't start with a heading or a quote."

    @pytest.mark.parametrize("text", ["line one\nline two", "tab\tseparated", "sep\u2028arated"])
    def test_single_line(self, text):
        assert _issue([text]) == "Each message has to be a single line of text."

    def test_unknown_placeholders_are_named(self):
        issue = _issue(["{player_name} {video_url} {image_url}"])
        assert issue.startswith("Unknown placeholders {image_url}, {video_url}.")
        assert "{killer}" in issue

    @pytest.mark.parametrize("text", [
        "{player_name} lost {value_lost} (kept {value_kept}) to a level {killer_combat_level} {killer}",
        "{player_name} died at {location}",
        "{player_name} vs {source} in {region_name}",  # the group messages' names
        "Mr. Mordaut got {player_name} again — 4.2M gone, e.g. everything",
        "{player_name} **really** thought that was ||safe||",
    ])
    def test_ordinary_messages_pass(self, text):
        assert _issue([text]) is None


class TestParseStoredMessages:
    def test_tolerates_garbage(self):
        assert mm.parse_stored_messages(None) == []
        assert mm.parse_stored_messages("not json") == []
        assert mm.parse_stored_messages('{"a": 1}') == []

    def test_drops_entries_todays_rules_refuse(self):
        raw = json.dumps(["ok {killer}", 5, "", "https://scam.example.com", "@everyone", "ok {killer}"])
        assert mm.parse_stored_messages(raw) == ["ok {killer}"]

    def test_caps_at_the_limit(self):
        raw = json.dumps([f"message {i}" for i in range(9)])
        assert len(mm.parse_stored_messages(raw)) == mm.MAX_MESSAGES


class TestChooseDeathTemplate:
    values = {
        "{player_name}": "**Alice**", "{killer}": "Zulrah", "{source}": "Zulrah",
        "{location}": "", "{region_name}": "", "{value_lost}": "", "{video_link}": "",
    }

    def test_member_beats_group(self):
        assert mm.choose_death_template(["{player_name} m"], ["{player_name} g"], self.values) == (
            "{player_name} m", "member")

    def test_group_when_member_has_none(self):
        assert mm.choose_death_template([], ["{player_name} g"], self.values) == (
            "{player_name} g", "group")

    def test_default_when_nobody_has_one(self):
        assert mm.choose_death_template([], [], self.values) == (None, "default")

    def test_blank_death_details_skip_a_template(self):
        member = ["{player_name} died at {location}", "{player_name} lost {value_lost}"]
        group = ["{player_name} fed {killer}"]
        assert mm.choose_death_template(member, group, self.values) == (
            "{player_name} fed {killer}", "group")

    def test_blank_media_tokens_do_not_skip(self):
        assert mm.choose_death_template([], ["{player_name} died {video_link}"], self.values) == (
            "{player_name} died {video_link}", "group")

    def test_picks_only_among_renderable(self):
        member = ["{player_name} at {location}", "{player_name} vs {killer}"]
        for seed in range(20):
            template, _ = mm.choose_death_template(member, [], self.values, rng=random.Random(seed))
            assert template == "{player_name} vs {killer}"

    def test_seeded_pick_is_deterministic(self):
        member = ["a {killer}", "b {killer}", "c {killer}", "d {killer}"]
        picks = {mm.pick_template(member, self.values, rng=random.Random(7)) for _ in range(5)}
        assert len(picks) == 1


class TestRenderTemplate:
    values = {
        "{player_name}": "<@42> **Alice**",  # the sender's formatted name, ping included
        "{killer}": "@everyone <@&1> Vorkath https://evil.example.com",
        "{source}": "Vorkath",
        "{location}": "Ungael",
        "{value_lost}": "4.2M",
        "{video_url}": "https://cdn.example.com/v.mp4",
    }

    def test_member_template_fills_tokens_and_cleans_client_values(self):
        out = mm.render_template("{player_name} fed {killer} at {location}", self.values, member=True)
        # The player's own opted-in ping survives; the client-sent killer name
        # cannot ping anyone or carry a link.
        assert out == "<@42> **Alice** fed Vorkath at Ungael"

    def test_member_template_drops_tokens_it_may_not_use(self):
        out = mm.render_template("{player_name} {video_url} died", self.values, member=True)
        assert out == "<@42> **Alice** died"

    def test_member_template_is_sanitized_first(self):
        out = mm.render_template("# @here {player_name} https://x.example.com died", self.values, member=True)
        assert out == "<@42> **Alice** died"

    def test_a_value_is_never_read_as_a_placeholder(self):
        values = {**self.values, "{killer}": "{value_lost}"}
        out = mm.render_template("{player_name} fed {killer}", values, member=True)
        assert out.endswith("fed {value_lost}")

    def test_group_template_keeps_its_tokens_and_loses_only_pings(self):
        out = mm.render_template(
            "@everyone {player_name} died {video_url} {not_a_token}", self.values, member=False)
        assert out == "<@42> **Alice** died https://cdn.example.com/v.mp4 {not_a_token}"

    def test_client_values_are_cleaned_in_group_templates_too(self):
        out = mm.render_template("{player_name} vs {killer}", self.values, member=False)
        assert out == "<@42> **Alice** vs Vorkath"

    def test_sample_replacements_cover_every_token_and_alias(self):
        samples = mm.sample_replacements()
        assert set(samples) == set(mm.MEMBER_DEATH_TOKENS)
        assert samples["{source}"] == samples["{killer}"]


def _gc(monkeypatch, value=None, raises=False):
    import utils.group_config as gc

    def _get(session, group_id, key, default=None):
        if raises:
            raise RuntimeError("db down")
        assert key == mm.ALLOW_DEATH_CONFIG_KEY
        return value

    monkeypatch.setattr(gc, "get", _get)


class TestMemberDeathTemplatesForGroup:
    def test_allowed_and_not_blocked(self, monkeypatch):
        _gc(monkeypatch, "1")
        monkeypatch.setattr(mm, "is_member_blocked", lambda s, g, p: False)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == ["{player_name} m"]

    def test_not_allowed(self, monkeypatch):
        _gc(monkeypatch, "0")
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == []

    def test_unset_is_not_allowed(self, monkeypatch):
        _gc(monkeypatch, None)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == []

    def test_blocked(self, monkeypatch):
        _gc(monkeypatch, "true")
        monkeypatch.setattr(mm, "is_member_blocked", lambda s, g, p: True)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == []

    def test_a_failing_block_lookup_fails_closed(self, monkeypatch):
        _gc(monkeypatch, "1")

        def _boom(s, g, p):
            raise RuntimeError("db down")

        monkeypatch.setattr(mm, "is_member_blocked", _boom)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == []

    def test_a_failing_config_read_fails_closed(self, monkeypatch):
        _gc(monkeypatch, raises=True)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 5, 9) == []

    def test_id_zero_is_a_real_id(self, monkeypatch):
        _gc(monkeypatch, "1")
        monkeypatch.setattr(mm, "is_member_blocked", lambda s, g, p: False)
        monkeypatch.setattr(mm, "load_member_messages", lambda s, p: ["{player_name} m"])
        assert mm.member_death_templates_for_group(MagicMock(), 0, 0) == ["{player_name} m"]
        assert mm.member_death_templates_for_group(MagicMock(), None, 9) == []


class TestStoreMemberMessages:
    def test_creates_a_row(self, monkeypatch):
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, p, t: None)
        session = MagicMock()
        saved = mm.store_member_messages(
            session, 9, ["  {player_name} fed {killer} "], via=mm.VIA_WEB, user_id=0)
        assert saved == ["{player_name} fed {killer}"]
        row = session.add.call_args.args[0]
        assert json.loads(row.messages) == saved
        assert row.updated_via == "web"
        assert row.updated_by_user_id == 0
        session.flush.assert_called()
        session.commit.assert_not_called()

    def test_updates_an_existing_row(self, monkeypatch):
        row = SimpleNamespace(messages="[]", updated_via=None, updated_by_user_id=None)
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, p, t: row)
        session = MagicMock()
        mm.store_member_messages(session, 9, ["{player_name} planked"], via=mm.VIA_PLUGIN)
        assert json.loads(row.messages) == ["{player_name} planked"]
        assert row.updated_via == "plugin"
        session.add.assert_not_called()

    def test_clearing_deletes_the_row(self, monkeypatch):
        row = SimpleNamespace(messages='["x"]')
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, p, t: row)
        session = MagicMock()
        assert mm.store_member_messages(session, 9, ["", " "], via=mm.VIA_DISCORD) == []
        session.delete.assert_called_once_with(row)

    def test_invalid_input_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(mm, "load_member_message_row", lambda s, p, t: None)
        session = MagicMock()
        with pytest.raises(mm.MemberMessageError):
            mm.store_member_messages(session, 9, ["@everyone"], via=mm.VIA_WEB)
        session.add.assert_not_called()
        session.flush.assert_not_called()


def test_limits_payload_matches_the_constants():
    payload = mm.limits_payload()
    assert payload["max_messages"] == mm.MAX_MESSAGES == 5
    assert payload["max_length"] == mm.MAX_MESSAGE_LENGTH == 150
    assert [t["token"] for t in payload["tokens"]] == [d["token"] for d in mm.DEATH_TOKENS]
    # A copy: a caller editing the payload cannot change the rule.
    payload["tokens"][0]["token"] = "{changed}"
    assert mm.DEATH_TOKENS[0]["token"] == "{player_name}"
