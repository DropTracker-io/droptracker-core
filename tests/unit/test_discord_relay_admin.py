"""Staff relay admin (web153a): input cleaning for create/patch bodies."""
from __future__ import annotations

import pytest

from web_api.routes import discord_relay_admin as admin


def test_guild_ids_are_cleaned_and_deduped():
    assert admin._clean_guild_ids(["123", 123, " 456 "]) == ["123", "456"]
    assert admin._clean_guild_ids(None) is None
    assert admin._clean_guild_ids("123") == "bad"
    assert admin._clean_guild_ids(["abc"]) == "bad"
    assert admin._clean_guild_ids(["1" * 33]) == "bad"


def test_fields_only_include_what_was_sent():
    changes, err = admin._clean_fields({"label": "x" * 100, "status": "active"})
    assert err is None and changes == {"label": "x" * 64, "status": "active"}


@pytest.mark.parametrize("body,needle", [
    ({"features": ["events", "nope"]}, "features"),
    ({"features": "events"}, "features"),
    ({"status": "on"}, "status"),
    ({"hold_minutes": 1}, "hold_minutes"),
    ({"hold_minutes": "soon"}, "hold_minutes"),
    ({"application_id": "abc"}, "application_id"),
])
def test_bad_fields(body, needle):
    changes, err = admin._clean_fields(body)
    assert changes is None and needle in err


def test_blank_application_id_clears_it():
    assert admin._clean_fields({"application_id": ""})[0] == {"application_id": None}


def test_features_dedupe_in_order():
    assert admin._clean_fields({"features": ["notifications", "events", "events"]})[0] == \
        {"features": ["notifications", "events"]}
