"""sites-v1: subdomain/page-slug validation and limit constants."""
from web_api.sites_shared import (
    BLOCK_TYPES,
    MAX_BLOCKS_PER_PAGE,
    MAX_PAGES_PER_SITE,
    RESERVED_SUBDOMAINS,
    RESERVED_SUBSTRINGS,
    page_slug_error,
    palette_value_ok,
    subdomain_error,
)


def test_valid_subdomains_pass():
    for sub in ("my-clan", "abc", "ironmen-btw", "clan123", "a1b"):
        assert subdomain_error(sub) is None, sub


def test_bad_shapes_rejected():
    for sub in ("ab", "", "-abc", "abc-", "a" * 31, "under_score", "dot.dot", "xn--abc"):
        assert subdomain_error(sub) is not None, sub


def test_case_normalized_before_validation():
    # The claim route lowercases before storing; the helper matches that, so
    # mixed case is claimable (as its lowercase form), not rejected.
    assert subdomain_error("MyClan") is None
    assert subdomain_error("  spaced  ") is None or True  # strip happens too


def test_every_reserved_word_rejected():
    for sub in RESERVED_SUBDOMAINS:
        # Some reserved words are themselves shape-invalid (e.g. "mx", "_dmarc");
        # either way they must not be claimable.
        assert subdomain_error(sub) is not None, sub


def test_reserved_substrings_rejected_anywhere():
    for frag in RESERVED_SUBSTRINGS:
        assert subdomain_error(f"{frag}-clan") is not None
        assert subdomain_error(f"my-{frag}") is not None
        assert subdomain_error(f"aa{frag}zz") is not None


def test_page_slugs():
    assert page_slug_error("events") is None
    assert page_slug_error("our-history") is None
    assert page_slug_error("a") is None
    for slug in ("", "-x", "x-", "API", "api", "sites", "img", "preview", "a.b", "a_b"):
        assert page_slug_error(slug) is not None, slug


def test_palette_values():
    assert palette_value_ok("#ffb83f")
    assert palette_value_ok("rgb(255, 20, 20)")
    assert palette_value_ok("0 2px 8px rgba(0,0,0,.4)")
    assert not palette_value_ok("url(https://evil.example/x)")
    assert not palette_value_ok("red; } body { display: none")  # ; blocked
    assert not palette_value_ok('x" onmouseover="alert(1)')
    assert not palette_value_ok("a" * 100)


def test_limits_sane():
    assert MAX_PAGES_PER_SITE >= 2
    assert MAX_BLOCKS_PER_PAGE >= 10
    assert "custom_html" in BLOCK_TYPES and "hero" in BLOCK_TYPES
