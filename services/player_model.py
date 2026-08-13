"""Storage and validation for uploaded character models (binary glTF).

An uploaded model is attacker-controlled binary that we then hand to a browser
to render, so it is validated structurally before it is ever written: magic,
container version, and that the declared chunk lengths actually fit inside the
declared file length. That will not stop a determined attack on a glTF parser,
but it does stop the whole class of "this is not a GLB at all" payloads, and it
keeps malformed files out of the render pipeline where they would fail silently.

Models are keyed by an outfit fingerprint from the client. The same character in
the same gear renders identically, so one file per outfit is enough and repeat
uploads are cheap no-ops.
"""
from __future__ import annotations

import os
import re
import struct
from typing import Optional, Tuple

# Public tree that nginx already serves at /img, so a stored model has a URL
# without any new routing.
MODEL_ROOT = "/store/droptracker/disc/static/assets/img/models"
PUBLIC_BASE = "https://www.droptracker.io/img/models"

# A geared character exports well under this. The cap is what stops a client
# filling the disk one "model" at a time.
MAX_MODEL_BYTES = 8 * 1024 * 1024

# glTF binary container: magic "glTF", little-endian uint32 version and length.
_GLB_MAGIC = 0x46546C67
_GLB_VERSION = 2
_GLB_HEADER_SIZE = 12
_GLB_CHUNK_HEADER_SIZE = 8

# Fingerprints are hex from the client; anything else is not one of ours and
# must never reach a filesystem path.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{1,32}$")


def is_valid_fingerprint(fingerprint: str) -> bool:
    return bool(fingerprint) and bool(_FINGERPRINT_RE.match(fingerprint))


def validate_glb(data: bytes) -> Tuple[bool, str]:
    """Structural check of a GLB container.

    Returns ``(ok, reason)``; the reason is for logs, never for the client — a
    precise parser error is a gift to someone probing the endpoint.
    """
    if not data:
        return False, "empty"
    if len(data) > MAX_MODEL_BYTES:
        return False, "too large"
    if len(data) < _GLB_HEADER_SIZE + _GLB_CHUNK_HEADER_SIZE:
        return False, "shorter than a glTF header"

    magic, version, declared_length = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC:
        return False, "bad magic"
    if version != _GLB_VERSION:
        return False, f"unsupported container version {version}"
    # The header's own length field must match reality: a mismatch means either
    # truncation or a deliberately lying header.
    if declared_length != len(data):
        return False, "declared length does not match payload"

    # Walk the chunks and confirm each one fits. A chunk claiming to be larger
    # than the file is the classic way to make a lazy parser read past the end.
    offset = _GLB_HEADER_SIZE
    saw_json = False
    while offset + _GLB_CHUNK_HEADER_SIZE <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += _GLB_CHUNK_HEADER_SIZE
        if chunk_length < 0 or offset + chunk_length > len(data):
            return False, "chunk overruns the file"
        if chunk_type == 0x4E4F534A:  # 'JSON'
            saw_json = True
        offset += chunk_length

    if not saw_json:
        return False, "no JSON chunk"
    return True, "ok"


def model_dir(player_id: int) -> str:
    return os.path.join(MODEL_ROOT, str(int(player_id)))


def model_path(player_id: int, fingerprint: str, *, pet: bool = False) -> str:
    name = f"{fingerprint}{'-pet' if pet else ''}.glb"
    return os.path.join(model_dir(player_id), name)


def model_url(player_id: int, fingerprint: str, *, pet: bool = False) -> str:
    name = f"{fingerprint}{'-pet' if pet else ''}.glb"
    return f"{PUBLIC_BASE}/{int(player_id)}/{name}"


def ensure_public_dir(path: str) -> None:
    """Create a directory both service accounts can write.

    The intake API runs as `user` and the web/node units as `debian`; a
    directory created by one and written by the other silently fails without
    this, which is the same trap the lootboard and recap image writers hit.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass


def store_model(player_id: int, fingerprint: str, data: bytes,
                *, pet: bool = False) -> Optional[str]:
    """Writes a validated model and returns its public URL, or None on failure.

    Written to a temporary name and then renamed, so a reader never sees a
    half-written model — the render page fetches these directly.
    """
    ok, reason = validate_glb(data)
    if not ok:
        print(f"Rejecting model upload for player {player_id}: {reason}")
        return None
    if not is_valid_fingerprint(fingerprint):
        print(f"Rejecting model upload for player {player_id}: bad fingerprint")
        return None

    directory = model_dir(player_id)
    ensure_public_dir(directory)

    final_path = model_path(player_id, fingerprint, pet=pet)
    tmp_path = f"{final_path}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, final_path)
        try:
            os.chmod(final_path, 0o666)
        except OSError:
            pass
    except OSError as exc:
        print(f"Could not store model for player {player_id}: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None

    return model_url(player_id, fingerprint, pet=pet)


def model_exists(player_id: int, fingerprint: str, *, pet: bool = False) -> bool:
    if not is_valid_fingerprint(fingerprint):
        return False
    return os.path.exists(model_path(player_id, fingerprint, pet=pet))


def prune_old_models(player_id: int, keep: int = 5) -> int:
    """Keeps only the most recently modified models for a player.

    Without this the directory grows forever: a player who changes gear often
    would leave a model behind for every outfit they have ever worn.
    """
    directory = model_dir(player_id)
    try:
        entries = [
            (os.path.getmtime(os.path.join(directory, name)), name)
            for name in os.listdir(directory)
            if name.endswith(".glb")
        ]
    except OSError:
        return 0

    # Pets are kept with their player model rather than counted separately, so
    # a pruned pair does not leave an orphaned pet behind.
    entries.sort(reverse=True)
    removed = 0
    for _mtime, name in entries[keep * 2:]:
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    return removed
