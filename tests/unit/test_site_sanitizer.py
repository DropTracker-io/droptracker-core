"""sites-v1: raw HTML/CSS sanitizer round-trips against hostile payloads.

The contract under test: sanitize_html strips every scripting vector while
keeping benign markup; sanitize_css rejects (never rewrites) external fetches
and scopes every selector under #site-root.
"""
import pytest

from web_api.site_sanitizer import (
    CssValidationError,
    sanitize_css,
    sanitize_html,
)


# --- HTML -------------------------------------------------------------------

HOSTILE_HTML = [
    ("<script>alert(1)</script>", "<script"),
    ('<img src=x onerror="alert(1)">', "onerror"),
    ('<a href="javascript:alert(1)">x</a>', "javascript:"),
    ('<a href="data:text/html,<script>x</script>">x</a>', "data:"),
    ('<div style="background:url(https://evil.example)">x</div>', "style="),
    ("<style>body{display:none}</style>", "<style"),
    ('<iframe src="https://evil.example"></iframe>', "<iframe"),
    ("<object data='x'></object>", "<object"),
    ("<embed src='x'>", "<embed"),
    ("<form action='https://evil.example'><input name=pw></form>", "<form"),
    ('<meta http-equiv="refresh" content="0;url=https://evil.example">', "<meta"),
    ('<base href="https://evil.example/">', "<base"),
    ("<svg><script>alert(1)</script></svg>", "<svg"),
    ('<div id="location">clobber</div>', "id="),
    ("<details open ontoggle=alert(1)>x</details>", "ontoggle"),
    ('<math><mi xlink:href="javascript:alert(1)">x</mi></math>', "xlink"),
]


@pytest.mark.parametrize("payload,marker", HOSTILE_HTML)
def test_hostile_html_neutralized(payload, marker):
    out = sanitize_html(payload)
    assert marker.lower() not in out.lower(), f"{marker!r} survived: {out!r}"


def test_benign_html_survives():
    src = (
        '<h2>Raids</h2><p class="intro">We raid <strong>nightly</strong>.</p>'
        '<table><tr><th scope="col">Boss</th><td colspan="2">Zulrah</td></tr></table>'
        '<img src="/img/npcdb/2042.png" alt="Zulrah" width="64" height="64">'
        '<a href="https://wiseoldman.net/x">WOM</a>'
    )
    out = sanitize_html(src)
    for frag in ("<h2>", "intro", "<strong>", "scope", "colspan", "/img/npcdb/2042.png", "wiseoldman"):
        assert frag in out
    # rel is forced on every link
    assert 'rel="noopener noreferrer nofollow ugc"' in out


def test_sanitize_is_idempotent():
    src = '<p onclick="x()">hi</p><a href="https://a.b">x</a>'
    once = sanitize_html(src)
    assert sanitize_html(once) == once


def test_http_links_stripped_https_kept():
    out = sanitize_html('<a href="http://insecure.example">a</a><a href="https://ok.example">b</a>')
    assert "http://insecure.example" not in out
    assert "https://ok.example" in out


# --- CSS --------------------------------------------------------------------


def test_css_scoping():
    out = sanitize_css(".a { color: red } .b, .c { margin: 0 }")
    assert "#site-root .a" in out
    assert "#site-root .b, #site-root .c" in out


def test_css_scoping_inside_media():
    out = sanitize_css("@media (max-width: 640px) { .m { display: none } }")
    assert "@media" in out and "#site-root .m" in out


def test_css_import_rejected():
    with pytest.raises(CssValidationError) as e:
        sanitize_css('@import url("https://evil.example/x.css");')
    assert any("@import" in p for p in e.value.problems)


def test_css_font_face_rejected():
    with pytest.raises(CssValidationError):
        sanitize_css("@font-face { font-family: x; src: url(https://evil.example/f.woff2) }")


def test_css_external_url_rejected_with_line():
    with pytest.raises(CssValidationError) as e:
        sanitize_css(".x { color: red }\n.y { background: url(https://evil.example/b.png) }")
    assert any("line 2" in p for p in e.value.problems)


def test_css_firstparty_and_relative_urls_pass():
    out = sanitize_css(
        ".x { background: url(/img/bg.png) }\n"
        ".y { background: url(https://www.droptracker.io/img/a.png) }\n"
        ".z { background: url(https://videos.droptracker.io/dt_uploads/b.webp) }"
    )
    assert out.count("url(") == 3


def test_css_expression_rejected():
    with pytest.raises(CssValidationError):
        sanitize_css(".x { width: expression(alert(1)) }")


def test_css_overlays_allowed():
    # Deliberate policy: own-page cosmetics (incl. fixed overlays) are allowed;
    # abuse is a ToS/kill-switch matter, not a parser matter.
    out = sanitize_css(".o { position: fixed; inset: 0 } .p::before { content: 'x' }")
    assert "position: fixed" in out


def test_css_unclosed_block_autocloses_scoped():
    # Per the CSS spec (and every browser), EOF closes open blocks — tinycss2
    # does the same, so an unclosed rule is not an error; it must still come
    # out scoped.
    out = sanitize_css(".x { color: red ")
    assert out.startswith("#site-root .x")
