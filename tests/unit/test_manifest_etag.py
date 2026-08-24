"""If-None-Match handling for ``GET /manifest``.

The endpoint has always advertised an ETag and had a 304 branch, and the branch
has never once been taken in production. nginx rewrites a strong ETag to its
weak form when it gzips a response, so a client echoes back ``W/"abc"`` while
the route compared against ``"abc"`` with ``==``. Every revalidation re-sent the
whole body, and nothing anywhere reported a problem.
"""
import pytest

from api.routes.manifest import _etag_matches


VERSION = "71c8790895b1"


class TestWeakComparison:
    def test_matches_the_weak_form_nginx_sends_back(self):
        """The regression. gzip is negotiated by every real client, so this is
        the *normal* case, not an edge case."""
        assert _etag_matches(f'W/"{VERSION}"', VERSION)

    def test_matches_the_strong_form(self):
        assert _etag_matches(f'"{VERSION}"', VERSION)

    def test_rejects_a_different_version(self):
        assert not _etag_matches('W/"0000deadbeef"', VERSION)

    def test_matches_when_listed_among_several(self):
        assert _etag_matches(f'"old1", W/"{VERSION}", "old2"', VERSION)

    def test_star_matches_any_representation(self):
        assert _etag_matches("*", VERSION)


class TestAbsentOrJunkHeaders:
    @pytest.mark.parametrize("header", [None, "", "   "])
    def test_no_header_is_not_a_match(self, header):
        """A first request must get a body, not a 304 it cannot render."""
        assert not _etag_matches(header, VERSION)

    def test_unquoted_junk_is_not_a_match(self):
        assert not _etag_matches("garbage", VERSION)

    def test_unquoted_version_is_accepted(self):
        """werkzeug's parser is lenient about the quoting RFC 7232 requires.

        Pinned rather than fought: we never emit this form, and a client that
        sends the right version in the wrong syntax genuinely does hold the
        right version, so a 304 is the honest answer.
        """
        assert _etag_matches(VERSION, VERSION)
