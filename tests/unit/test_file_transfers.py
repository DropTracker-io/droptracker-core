"""File transfers (web95a): the sanitisers standing between arbitrary user
uploads and the response headers they end up in.

``/file-transfer`` is the one endpoint that accepts *any* file type from any
signed-in user, so nothing about a stored name or MIME type may reach a
response header unexamined. Three properties are what keep that safe, and each
is a real attack if it slips:

* **Names can't break their header.** A quote, a newline or a path separator in
  ``filename`` would either split the Content-Disposition header or hand the
  recipient a path instead of a name.
* **Types are allowlisted, not sanitised.** A declared MIME we don't recognise
  becomes ``application/octet-stream`` rather than being cleaned up and echoed,
  so no crafted value can reach the header at all.
* **Inline rendering is a closed set.** Anything outside it downloads. SVG and
  HTML in particular execute script in our own origin, so they must never come
  back inline no matter what the caller asks for.

Both modules import from ``db``, which the conftest stubs as a MagicMock — fine
for the pure helpers here, except that ``TRANSFER_RETENTION_DAYS`` then arrives
as a mock instead of an int, so the expiry test patches a real value in (the
stubbed-constants trap CLAUDE.md calls out).
"""

from datetime import datetime, timedelta

import pytest

import scripts.prune_file_transfers as prune
import web_api.routes.file_transfers as ft


# --------------------------------------------------------------------------- #
# safe_filename
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        # Windows and POSIX pathfuls: only the last component is a name.
        (r"C:\Users\bob\secrets.xlsx", "secrets.xlsx"),
        ("/etc/passwd", "passwd"),
        ("../../../../etc/shadow", "shadow"),
        # Header-injection vectors.
        ('evil";\r\nX-Injected: 1', "evil;X-Injected: 1"),
        ("line\nbreak.txt", "linebreak.txt"),
        # A leading dot would save as a hidden file on the recipient's box.
        (".bashrc", "bashrc"),
        # Nothing usable left -> a name we chose, never an empty header.
        ("", "upload.bin"),
        (None, "upload.bin"),
        ("...", "upload.bin"),
        ("/", "upload.bin"),
        # Non-ASCII survives intact; the header encodes it separately.
        ("réponse finale.txt", "réponse finale.txt"),
    ],
)
def test_safe_filename(raw, expected):
    assert ft.safe_filename(raw) == expected


def test_safe_filename_is_length_bounded():
    """The column is VARCHAR(255); a longer name must be cut, not rejected."""
    assert len(ft.safe_filename("a" * 400 + ".txt")) == 255


# --------------------------------------------------------------------------- #
# safe_content_type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("application/pdf", "application/pdf"),
        ("IMAGE/PNG", "image/png"),
        # Parameters are dropped, not preserved — charset can carry surprises.
        ("text/plain; charset=utf-8", "text/plain"),
        ("  application/zip  ", "application/zip"),
        # Anything malformed falls back rather than being repaired.
        ("not-a-mime", "application/octet-stream"),
        ("text/html\r\nX-Injected: 1", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
        ("a/" + "b" * 200, "application/octet-stream"),
    ],
)
def test_safe_content_type(raw, expected):
    assert ft.safe_content_type(raw) == expected


# --------------------------------------------------------------------------- #
# disposition
# --------------------------------------------------------------------------- #
def test_disposition_defaults_to_attachment():
    header = ft.disposition("notes.txt", "text/plain", want_inline=False)
    assert header.startswith('attachment; filename="notes.txt"')


def test_disposition_allows_inline_for_inert_types():
    header = ft.disposition("shot.png", "image/png", want_inline=True)
    assert header.startswith("inline;")


@pytest.mark.parametrize(
    "content_type",
    ["image/svg+xml", "text/html", "application/xhtml+xml", "application/octet-stream"],
)
def test_disposition_refuses_inline_for_scriptable_types(content_type):
    """Asking for a preview must not be enough to get one.

    SVG and HTML render script in our own origin — served inline they turn a
    file drop-box into stored XSS against droptracker.io.
    """
    header = ft.disposition("payload", content_type, want_inline=True)
    assert header.startswith("attachment;")


def test_disposition_encodes_non_ascii_names_twice():
    """A plain ASCII fallback plus RFC 5987, so old clients still get a name."""
    header = ft.disposition("réponse.txt", "text/plain", want_inline=False)
    assert 'filename="r_ponse.txt"' in header
    assert "filename*=UTF-8''r%C3%A9ponse.txt" in header


# --------------------------------------------------------------------------- #
# Storage keys and retention
# --------------------------------------------------------------------------- #
def test_new_storage_key_is_namespaced_and_unique():
    """The B2 application key is namePrefix-restricted to ``dt_`` — a key
    outside that namespace 403s as 'not entitled' at upload time."""
    keys = {ft.new_storage_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(k.startswith("dt_transfers/") for k in keys)


def test_expiry_is_thirty_days_out(monkeypatch):
    # Real int required: the conftest's `db` stub hands the module a MagicMock
    # for this constant, which timedelta() will not take.
    monkeypatch.setattr(ft, "TRANSFER_RETENTION_DAYS", 30)
    now = datetime(2026, 8, 13, 12, 0, 0)
    assert ft.expiry_from(now) == now + timedelta(days=30)


def test_preview_flag_matches_the_inline_allowlist():
    """`can_preview` is what the UI renders a View button from, so it has to
    agree with what `disposition` will actually do."""
    for content_type in ft._INLINE_SAFE_TYPES:
        assert ft.disposition("f", content_type, want_inline=True).startswith("inline;")


# --------------------------------------------------------------------------- #
# Pruner guard rails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key,prunable",
    [
        ("dt_transfers/deadbeef", True),
        # Proof screenshots and videos share the bucket — never ours to delete.
        ("dt_uploads/deadbeef.png", False),
        ("videos/12345/clip.mp4", False),
        ("dt_transfers/../dt_uploads/x.png", False),
        ("", False),
        (None, False),
    ],
)
def test_pruner_only_touches_its_own_namespace(key, prunable):
    assert prune.is_prunable_key(key) is prunable


def test_pruner_cutoff_respects_grace_days():
    now = datetime(2026, 8, 13, 4, 30, 0)
    assert prune.cutoff(now, 0) == now
    assert prune.cutoff(now, 3) == now - timedelta(days=3)
