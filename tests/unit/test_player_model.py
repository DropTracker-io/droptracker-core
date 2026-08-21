"""Validation of uploaded character models.

The bytes come from a game client we do not control and are then handed to a
browser to render, so the interesting cases are all the ways a "model" can fail
to be one.
"""
import struct

from services.player_model import (
    MAX_MODEL_BYTES,
    is_valid_fingerprint,
    validate_glb,
)

GLB_MAGIC = 0x46546C67
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942


def build_glb(*, magic=GLB_MAGIC, version=2, chunks=None, declared_length=None) -> bytes:
    """Assembles a GLB container, so tests can bend exactly one field."""
    if chunks is None:
        chunks = [(JSON_CHUNK, b'{"asset":{"version":"2.0"}}')]

    body = b""
    for chunk_type, payload in chunks:
        # glTF requires 4-byte alignment; pad so the fixtures are realistic.
        padding = (-len(payload)) % 4
        padded = payload + b" " * padding
        body += struct.pack("<II", len(padded), chunk_type) + padded

    total = 12 + len(body) if declared_length is None else declared_length
    return struct.pack("<III", magic, version, total) + body


class TestValidateGlb:
    def test_accepts_a_well_formed_model(self):
        ok, reason = validate_glb(build_glb())
        assert ok, reason

    def test_accepts_json_plus_binary_chunks(self):
        data = build_glb(chunks=[
            (JSON_CHUNK, b'{"asset":{"version":"2.0"}}'),
            (BIN_CHUNK, b"\x00" * 64),
        ])
        assert validate_glb(data)[0]

    def test_rejects_empty_and_tiny_payloads(self):
        assert not validate_glb(b"")[0]
        assert not validate_glb(b"glTF")[0]

    def test_rejects_wrong_magic(self):
        """A PNG, a zip or a script renamed to .glb must not get through."""
        assert not validate_glb(build_glb(magic=0x89504E47))[0]

    def test_rejects_unsupported_container_version(self):
        assert not validate_glb(build_glb(version=1))[0]

    def test_rejects_length_that_disagrees_with_the_payload(self):
        # Truncated upload, or a header deliberately lying about its size.
        assert not validate_glb(build_glb(declared_length=999_999))[0]

    def test_rejects_a_chunk_that_overruns_the_file(self):
        """The classic parser attack: a chunk claiming to be bigger than the
        file, so a naive reader walks off the end."""
        good = build_glb()
        # Rewrite the first chunk's length to something enormous.
        tampered = bytearray(good)
        struct.pack_into("<I", tampered, 12, 10_000_000)
        ok, reason = validate_glb(bytes(tampered))
        assert not ok
        assert "overrun" in reason

    def test_rejects_a_container_with_no_json_chunk(self):
        assert not validate_glb(build_glb(chunks=[(BIN_CHUNK, b"\x00" * 32)]))[0]

    def test_rejects_oversized_uploads(self):
        assert not validate_glb(b"\x00" * (MAX_MODEL_BYTES + 1))[0]


class TestFingerprint:
    def test_accepts_lowercase_hex(self):
        assert is_valid_fingerprint("1a2b3c4d")

    def test_rejects_path_traversal(self):
        """The fingerprint becomes part of a filename, so this is the one that
        actually matters."""
        assert not is_valid_fingerprint("../../etc/passwd")
        assert not is_valid_fingerprint("a/b")
        assert not is_valid_fingerprint("..")

    def test_rejects_empty_uppercase_and_overlong(self):
        assert not is_valid_fingerprint("")
        assert not is_valid_fingerprint("ABCDEF")
        assert not is_valid_fingerprint("a" * 33)
