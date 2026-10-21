"""web138a: channel config keys only store a channel id."""
import pytest

from web_api.config_registry import ConfigValidationError, coerce_to_storage, parse_channel_ref

ID = "1196157662371516557"


@pytest.mark.parametrize("raw", [
    ID, int(ID), f"<#{ID}>", f"  {ID}  ",
    f"https://discord.com/channels/1195743428877766836/{ID}",
    f"https://discord.com/channels/1195743428877766836/{ID}/1300000000000000000",
    f"https://ptb.discord.com/channels/1195743428877766836/{ID}",
    f"https://discordapp.com/channels/1195743428877766836/{ID}/",
])
def test_channel_refs_parse_to_the_id(raw):
    assert parse_channel_ref(raw) == ID
    assert coerce_to_storage("channel_id_to_post_loot", raw) == ID


@pytest.mark.parametrize("raw", [None, "", "0", "  "])
def test_empty_clears(raw):
    assert coerce_to_storage("channel_id_to_post_loot", raw) == ""


@pytest.mark.parametrize("raw", [
    "drop-tracker", "#loot-board", "loot board",
    "https://discord.com/api/webhooks/123/abc", "https://example.com/channels/1/2", "12345", True,
])
def test_names_and_junk_are_refused(raw):
    with pytest.raises(ConfigValidationError):
        coerce_to_storage("channel_id_to_post_loot", raw)


def test_every_channel_key_is_validated():
    from web_api.config_registry import GROUP_CONFIG_FIELDS as CONFIG_FIELDS

    channel_keys = [f["key"] for f in CONFIG_FIELDS if f["type"] == "channel"]
    assert "lootboard_channel_id" in channel_keys
    for key in channel_keys:
        with pytest.raises(ConfigValidationError):
            coerce_to_storage(key, "general")
