"""Group-editable message text and footers (services/notification_text.py, web140a)."""

import sys
from types import SimpleNamespace

import pytest

nt = sys.modules["services.notification_text"]


@pytest.fixture(autouse=True)
def _fresh_caches():
    nt.invalidate()
    yield
    nt.invalidate()


# --- validation --------------------------------------------------------------

def test_content_none_keeps_the_built_in_line_and_blank_means_no_text():
    assert nt.validate_content(None) is None
    assert nt.validate_content("   ") == ""
    assert nt.validate_content("  {player_mention} got loot  ") == "{player_mention} got loot"


@pytest.mark.parametrize("text", ["@everyone drop!", "hey @here", "@EVERYONE"])
def test_content_refuses_mass_mentions(text):
    with pytest.raises(ValueError, match="everyone or @here"):
        nt.validate_content(text)


def test_content_allows_role_and_user_mentions():
    assert nt.validate_content("<@&123> {player_mention}") == "<@&123> {player_mention}"


def test_content_length_cap():
    with pytest.raises(ValueError):
        nt.validate_content("x" * (nt.CONTENT_MAX + 1))


def test_footer_validation():
    assert nt.validate_footer("none", "ignored") == ("none", None)
    assert nt.validate_footer("default", None) == ("default", None)
    assert nt.validate_footer("custom", " Clan X ") == ("custom", "Clan X")
    with pytest.raises(ValueError):
        nt.validate_footer("custom", "  ")
    with pytest.raises(ValueError):
        nt.validate_footer("inherit", None)


# --- rendering ---------------------------------------------------------------

def test_default_content_matches_the_old_sender_lines():
    # The senders' f-strings these replaced, verbatim.
    assert nt.default_content("drop", "NAME") == "NAME received a drop:"
    assert nt.default_content("pb", "NAME") == "NAME has achieved a new personal best:"
    assert nt.default_content("pet_dupe", "NAME") == "NAME has received a duplicate pet!"
    assert nt.default_content("pvp_death", "NAME") == "NAME was killed in PvP!"
    assert "lb" not in nt.DEFAULT_CONTENT


def test_render_content_fills_tokens_and_the_mention():
    out = nt.render_content(
        "{player_mention} got {item_name} ({item_value})",
        {"{item_name}": "Twisted bow", "{item_value}": "`1.2B`"},
        "<@42> ([Ron](url))",
    )
    assert out == "<@42> ([Ron](url)) got Twisted bow (`1.2B`)"


def test_render_content_drops_lines_with_missing_values():
    out = nt.render_content(
        "{player_mention} got {item_name}\nKC: {kill_count}",
        {"{item_name}": "Pet", "{kill_count}": "None"},
        "Ron",
    )
    assert out == "Ron got Pet"


def test_render_content_blank_result_sends_no_text():
    assert nt.render_content("", {}, "Ron") is None
    assert nt.render_content("KC: {kill_count}", {"{kill_count}": None}, "Ron") is None


def test_render_content_neutralizes_a_mass_mention_that_got_in():
    out = nt.render_content("@everyone {player_mention}", {}, "Ron")
    assert "@everyone" not in out and out.endswith("Ron")


# --- message text lookup -----------------------------------------------------

def _rows(monkeypatch, table):
    calls = []

    def fake(group_id, embed_type):
        calls.append((group_id, embed_type))
        if (group_id, embed_type) in table:
            return True, table[(group_id, embed_type)]
        return False, None

    monkeypatch.setattr(nt, "_row_content", fake)
    return calls


def test_own_text_needs_custom_embeds(monkeypatch):
    _rows(monkeypatch, {(5, "drop"): "mine"})
    assert nt.content_template(5, "drop", True) == "mine"
    nt.invalidate()
    assert nt.content_template(5, "drop", False) is None


def test_null_own_text_falls_back_to_staff_default_then_sender(monkeypatch):
    _rows(monkeypatch, {(5, "drop"): None, (1, "drop"): "staff {player_mention}"})
    assert nt.content_template(5, "drop", True) == "staff {player_mention}"
    _rows(monkeypatch, {(5, "drop"): None, (1, "drop"): None})
    nt.invalidate()
    assert nt.content_template(5, "drop", True) is None


def test_empty_own_text_is_a_choice_not_a_fallback(monkeypatch):
    _rows(monkeypatch, {(5, "drop"): "", (1, "drop"): "staff"})
    assert nt.content_template(5, "drop", True) == ""


def test_duplicate_pet_follows_the_design_used(monkeypatch):
    calls = _rows(monkeypatch, {(5, "pet"): "pet text", (1, "pet_dupe"): "dupe default"})
    assert nt.content_template(5, "pet_dupe", True, own_type="pet") == "pet text"
    assert (5, "pet") in calls


def test_content_lookup_is_cached(monkeypatch):
    calls = _rows(monkeypatch, {(5, "drop"): "mine"})
    nt.content_template(5, "drop", True)
    nt.content_template(5, "drop", True)
    assert calls.count((5, "drop")) == 1


def test_content_lookup_error_keeps_the_built_in_line(monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(nt, "_row_content", boom)
    assert nt.content_template(5, "drop", True) is None


# --- footer ------------------------------------------------------------------

def test_pick_footer_order():
    rows = {"": ("custom", "Clan"), "pb": ("none", None)}
    assert nt.pick_footer(rows, "pb", "DT") is None
    assert nt.pick_footer(rows, "drop", "DT") == "Clan"
    assert nt.pick_footer({}, "drop", "DT") == "DT"
    assert nt.pick_footer({"": ("none", None), "clog": ("default", None)}, "clog", "DT") == "DT"


def test_pick_footer_fallback_types():
    rows = {"pet": ("custom", "Pets!"), "level_up": ("none", None)}
    assert nt.pick_footer(rows, "pet_dupe", "DT") == "Pets!"
    assert nt.pick_footer(rows, "xp_milestone", "DT") is None
    assert nt.pick_footer({**rows, "pet_dupe": ("default", None)}, "pet_dupe", "DT") == "DT"


def _entitled(monkeypatch, value):
    monkeypatch.setattr(
        sys.modules["db.entitlements"], "group_has_entitlement",
        lambda gid, key: value if key == nt.FOOTER_ENTITLEMENT else False,
    )


def test_footer_needs_the_entitlement(monkeypatch):
    monkeypatch.setenv("DISCORD_MESSAGE_FOOTER", "DT footer")
    monkeypatch.setattr(nt, "_load_footer_rows", lambda gid: {"": ("none", None)})
    _entitled(monkeypatch, False)
    assert nt.footer_template(5, "drop") == "DT footer"
    _entitled(monkeypatch, True)
    assert nt.footer_template(5, "drop") is None


def test_footer_error_keeps_ours(monkeypatch):
    monkeypatch.setenv("DISCORD_MESSAGE_FOOTER", "DT footer")
    _entitled(monkeypatch, True)

    def boom(gid):
        raise RuntimeError("db down")

    monkeypatch.setattr(nt, "_load_footer_rows", boom)
    assert nt.footer_template(5, "drop") == "DT footer"


class _Embed:
    def __init__(self):
        self.footer = SimpleNamespace(text="old")

    def set_footer(self, text):
        self.footer = SimpleNamespace(text=text)


def test_apply_footer_fills_placeholders_and_clears_empty(monkeypatch):
    _entitled(monkeypatch, True)
    monkeypatch.setattr(nt, "_load_footer_rows", lambda gid: {
        "drop": ("custom", "{player_name_plain} · {month_name}"),
        "clog": ("custom", "{kill_count}"),
        "pb": ("none", None),
    })
    e = nt.apply_footer(_Embed(), 5, "drop", {"{player_name_plain}": "Ron", "{month_name}": "May"})
    assert e.footer.text == "Ron · May"
    assert nt.apply_footer(_Embed(), 5, "clog", {"{kill_count}": None}).footer is None
    assert nt.apply_footer(_Embed(), 5, "pb", {}).footer is None


# --- web_api validation ------------------------------------------------------

def test_embed_put_carries_message_text_for_text_types_only():
    from web_api.routes import embeds

    body = {"title": "t", "content": " {player_mention} got it "}
    assert embeds._validate_body(body, "drop")["content"] == "{player_mention} got it"
    assert embeds._validate_body(body, "lb")["content"] is None
    assert embeds._validate_body({"title": "t"}, "drop")["content"] is None


def test_embed_put_refuses_everyone_in_message_text():
    from web_api.common import ProblemException as HTTPException

    from web_api.routes import embeds

    with pytest.raises(HTTPException):
        embeds._validate_body({"title": "t", "content": "@everyone"}, "drop")


def test_footer_document_validation():
    from web_api.common import ProblemException as HTTPException

    from web_api.routes import notification_footers as nf

    out = nf._validate_body({
        "all": {"mode": "custom", "text": "Clan X"},
        "types": {"pb": {"mode": "none"}, "drop": None},
    })
    assert out == {"": ("custom", "Clan X"), "pb": ("none", None)}
    for bad in ({"types": {"nope": {"mode": "none"}}}, {"all": {"mode": "custom", "text": ""}}):
        with pytest.raises(HTTPException):
            nf._validate_body(bad)
