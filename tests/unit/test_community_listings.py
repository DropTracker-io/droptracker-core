"""/tools directory listings (web146a): what staff and maintainers may store and change."""
import json

import pytest

from web_api.community_listings import (
    ADMIN_ONLY_FIELDS,
    MAINTAINER_FIELDS,
    SECTIONS,
    ListingError,
    can_view_directory,
    check_edit_allowed,
    parse_json_list,
    slugify,
    validate_listing,
    visible_to,
)


def _full(**over):
    body = {
        "name": "Wise Old Man",
        "section": "progress",
        "summary": "Open-source progress tracker.",
        "links": [{"label": "wiseoldman.net", "href": "https://wiseoldman.net"}],
    }
    body.update(over)
    return body


class TestCreate:
    def test_minimal_listing_gets_defaults_and_derived_slug(self):
        fields = validate_listing(_full(), partial=False)
        assert fields["slug"] == "wise-old-man"
        assert fields["sort_order"] == 100
        assert fields["visible"] is True
        assert json.loads(fields["tags_json"]) == []
        assert json.loads(fields["links_json"])[0]["href"] == "https://wiseoldman.net"
        assert fields["details"] is None and fields["logo_url"] is None

    @pytest.mark.parametrize("missing", ["name", "summary", "section", "links"])
    def test_core_fields_required(self, missing):
        body = _full()
        del body[missing]
        with pytest.raises(ListingError):
            validate_listing(body, partial=False)

    def test_unknown_section(self):
        with pytest.raises(ListingError):
            validate_listing(_full(section="martians"), partial=False)

    def test_unknown_field_is_refused(self):
        with pytest.raises(ListingError):
            validate_listing(_full(created_by=0), partial=False)

    def test_name_without_letters_needs_explicit_slug(self):
        with pytest.raises(ListingError):
            validate_listing(_full(name="!!!"), partial=False)
        assert validate_listing(_full(name="!!!", slug="bang"), partial=False)["slug"] == "bang"

    @pytest.mark.parametrize("slug", ["Has-Caps", "two--dashes", "-lead", "sp ace", "a" * 65])
    def test_bad_slugs(self, slug):
        with pytest.raises(ListingError):
            validate_listing(_full(slug=slug), partial=False)


class TestLinksAndImages:
    @pytest.mark.parametrize("href", [
        "javascript:alert(1)",
        "data:text/html,hi",
        "//evil.example",
        "wiseoldman.net",
        "https://",
        "https://a b.com",
    ])
    def test_unsafe_or_incomplete_links_refused(self, href):
        with pytest.raises(ListingError):
            validate_listing(_full(links=[{"label": "x", "href": href}]), partial=False)

    def test_link_count_and_label_limits(self):
        many = [{"label": f"l{i}", "href": "https://a.com"} for i in range(7)]
        with pytest.raises(ListingError):
            validate_listing(_full(links=many), partial=False)
        with pytest.raises(ListingError):
            validate_listing(_full(links=[{"label": "", "href": "https://a.com"}]), partial=False)

    def test_logo_must_be_https(self):
        with pytest.raises(ListingError):
            validate_listing(_full(logo_url="http://a.com/logo.png"), partial=False)
        fields = validate_listing(_full(logo_url="https://a.com/logo.png"), partial=False)
        assert fields["logo_url"] == "https://a.com/logo.png"

    def test_blank_logo_clears_it(self):
        assert validate_listing({"logo_url": "  "}, partial=True) == {"logo_url": None}

    @pytest.mark.parametrize("accent", ["red", "#fff", "#12345g", "#123456;background:url(x)"])
    def test_accent_must_be_six_digit_hex(self, accent):
        # It ends up inside a style attribute, so nothing else gets through.
        with pytest.raises(ListingError):
            validate_listing({"accent": accent}, partial=True)

    def test_accent_is_lowercased(self):
        assert validate_listing({"accent": "#3B82F6"}, partial=True) == {"accent": "#3b82f6"}


class TestTags:
    def test_dedupes_case_insensitively_and_drops_blanks(self):
        fields = validate_listing({"tags": ["Open source", "open source", " ", "Prices"]}, partial=True)
        assert json.loads(fields["tags_json"]) == ["Open source", "Prices"]

    def test_tag_limits(self):
        with pytest.raises(ListingError):
            validate_listing({"tags": [f"t{i}" for i in range(9)]}, partial=True)
        with pytest.raises(ListingError):
            validate_listing({"tags": ["x" * 33]}, partial=True)


class TestPatch:
    def test_only_sent_fields_come_back(self):
        assert validate_listing({"summary": " New summary "}, partial=True) == {"summary": "New summary"}

    def test_empty_patch_refused(self):
        with pytest.raises(ListingError):
            validate_listing({}, partial=True)

    def test_required_text_cannot_be_blanked(self):
        with pytest.raises(ListingError):
            validate_listing({"name": "  "}, partial=True)

    def test_optional_text_can_be_cleared(self):
        assert validate_listing({"details": ""}, partial=True) == {"details": None}

    @pytest.mark.parametrize("order", [True, -1, 10000, "5", 1.5])
    def test_sort_order_must_be_int_in_range(self, order):
        with pytest.raises(ListingError):
            validate_listing({"sort_order": order}, partial=True)

    def test_visible_must_be_bool(self):
        with pytest.raises(ListingError):
            validate_listing({"visible": "yes"}, partial=True)


class TestPermissions:
    def test_field_sets_cover_everything_without_overlap(self):
        assert not (MAINTAINER_FIELDS & ADMIN_ONLY_FIELDS)

    def test_admin_can_change_anything(self):
        check_edit_allowed(ADMIN_ONLY_FIELDS | MAINTAINER_FIELDS, is_admin=True, is_maintainer=False)

    def test_maintainer_can_change_content(self):
        check_edit_allowed(MAINTAINER_FIELDS, is_admin=False, is_maintainer=True)

    @pytest.mark.parametrize("field", sorted(ADMIN_ONLY_FIELDS))
    def test_maintainer_cannot_change_placement_or_our_note(self, field):
        with pytest.raises(ListingError) as exc:
            check_edit_allowed(["summary", field], is_admin=False, is_maintainer=True)
        assert exc.value.status == 403

    def test_stranger_cannot_edit(self):
        with pytest.raises(ListingError) as exc:
            check_edit_allowed(["summary"], is_admin=False, is_maintainer=False)
        assert exc.value.status == 403

    def test_preview_page_open_to_staff_and_maintainers_only(self):
        assert can_view_directory(is_admin=True, maintains_any=False)
        assert can_view_directory(is_admin=False, maintains_any=True)
        assert not can_view_directory(is_admin=False, maintains_any=False)

    def test_hidden_listing_visibility(self):
        assert visible_to(True, is_admin=False, is_maintainer=False)
        assert visible_to(False, is_admin=False, is_maintainer=True)
        assert visible_to(False, is_admin=True, is_maintainer=False)
        assert not visible_to(False, is_admin=False, is_maintainer=False)


def test_slugify():
    assert slugify("Old School RuneScape Wiki") == "old-school-runescape-wiki"
    assert slugify("  OSRS  Clans!! ") == "osrs-clans"


def test_parse_json_list_fails_soft():
    assert parse_json_list(None) == []
    assert parse_json_list("not json") == []
    assert parse_json_list('{"a": 1}') == []
    assert parse_json_list('["a"]') == ["a"]


def test_sections_match_seed_migration():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic/versions/web146a_community_listings.py"
    spec = importlib.util.spec_from_file_location("web146a", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for row in mod.SEED:
        assert row["section"] in SECTIONS
        payload = {k: row[k] for k in ("name", "section", "summary", "links", "tags", "accent", "sort_order")}
        validate_listing(payload, partial=False)
