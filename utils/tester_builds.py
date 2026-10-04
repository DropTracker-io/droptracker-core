"""The tester client builds a private build job publishes to disk.

A build job outside this repo watches the plugin repository and RuneLite's
releases, builds a development client for Bug Testers, and leaves what it
built in one directory (``TESTER_BUILDS_DIR``):

    current.json             manifest of the current build (may be absent)
    status.json              the pipeline's last check (may be absent)
    builds/<build_id>.json   one manifest per published build (all kept)
    builds/<file>.zip        the zips (only the newest few are kept)

This module is the only reader of those files. The website routes
(web_api/routes/tester_builds.py) serve them, the submission path reads the
current manifest to tell a pre-release plugin version from a released one
(utils/plugin_versions.py), and the build job's reporter
(scripts/tester_build_report.py) reads them to post to Discord.

Two rules hold throughout:

* Nothing here raises. The files belong to another job and may be missing,
  half-written or edited by hand; a caller gets ``None`` or an empty list.
* Nothing read from disk is passed on as it was found. The ``*_payload``
  builders coerce every field to the type the API promises, and the zip's
  file name is checked before anyone is told to serve it.

Stdlib only at import, so it loads in every process and under the unit-test
conftest.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BUILDS_DIR = "/var/www/droptracker-tester-builds"

#: The only shape of zip name that is ever handed to a caller. The website
#: puts it in an X-Accel-Redirect header, so it must be a bare file name: no
#: separators, nothing a proxy could read as a path. Always tested with
#: ``fullmatch``: ``$`` alone also matches before a trailing newline.
SAFE_ZIP_RE = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
#: plugin_test_downloads.file_name is VARCHAR(160).
MAX_ZIP_NAME_LENGTH = 160

_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: plugin_test_downloads.build_id is VARCHAR(96).
MAX_BUILD_ID_LENGTH = 96

MAX_CHANGES = 200
MAX_POINTS = 20
_MAX_ERROR_CHARS = 2000
#: A manifest with 200 changes is well under 100 KB. Anything far past that
#: in this directory is not a manifest, and is not worth parsing to find out.
_MAX_JSON_BYTES = 4 * 1024 * 1024

_STATES = ("ok", "failed", "building")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INT_RE = re.compile(r"^-?\d{1,18}$")


def builds_dir() -> str:
    """Where the build job publishes. Read per call, so tests can move it."""
    return (os.getenv("TESTER_BUILDS_DIR") or "").strip() or DEFAULT_BUILDS_DIR


# --------------------------------------------------------------------------- #
# Reading files
# --------------------------------------------------------------------------- #
def _signature(path: str) -> Optional[Tuple[int, int, int]]:
    """What changes when a file is rewritten or replaced, or None if absent.

    The inode is part of it because the build job publishes by rename: a new
    file with the same size, written within the same clock tick as the old
    one, would otherwise look unchanged.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _parse_file(path: str) -> Tuple[Optional[Tuple[int, int, int]], Optional[dict]]:
    """``(signature, JSON object)`` for a file; the object is None for junk."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            signature = (st.st_mtime_ns, st.st_size, st.st_ino)
            if st.st_size > _MAX_JSON_BYTES:
                return signature, None
            raw = fh.read(_MAX_JSON_BYTES + 1)
    except OSError:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        data = None
    return signature, data if isinstance(data, dict) else None


#: path -> (signature, parsed object or None). Only current.json and
#: status.json go through it, so it holds two entries per directory.
_file_cache: Dict[str, Tuple[Tuple[int, int, int], Optional[dict]]] = {}


def read_json(path: str) -> Optional[dict]:
    """A JSON object from disk, re-parsed only when the file has changed.

    The submission path asks for the current manifest (see
    utils/plugin_versions.py), so an unchanged file must cost one ``stat``,
    not a parse. The object returned is the cached one: read it, never
    change it.
    """
    signature = _signature(path)
    if signature is None:
        _file_cache.pop(path, None)
        return None
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    signature, data = _parse_file(path)
    if signature is None:
        _file_cache.pop(path, None)
        return None
    _file_cache[path] = (signature, data)
    return data


def is_build_id(value: Any) -> bool:
    """Whether ``value`` is a build id plain enough to be a file name and a key."""
    return (isinstance(value, str) and len(value) <= MAX_BUILD_ID_LENGTH
            and _BUILD_ID_RE.fullmatch(value) is not None)


def _manifest(data: Optional[dict]) -> Optional[dict]:
    """``data`` if it is a manifest that names its build, otherwise None."""
    if isinstance(data, dict) and is_build_id(data.get("build_id")):
        return data
    return None


def load_current(root: Optional[str] = None) -> Optional[dict]:
    """The current build's manifest, or None when there is no usable one."""
    return _manifest(read_json(os.path.join(root or builds_dir(), "current.json")))


def load_status(root: Optional[str] = None) -> Optional[dict]:
    """The build pipeline's last status, or None."""
    return read_json(os.path.join(root or builds_dir(), "status.json"))


#: builds/ directory -> {file name: (signature, build time or None for junk)}.
#: Every manifest is kept, so the directory only grows. Remembering when each
#: build was made means a listing re-parses only the files that changed, then
#: reads just the few it returns. The manifests themselves are not held: a
#: year of them would be a lot of memory in every web worker.
_build_index: Dict[str, Dict[str, Tuple[Tuple[int, int, int], Optional[int]]]] = {}


def list_builds(limit: int = 20, root: Optional[str] = None) -> List[dict]:
    """Published builds' manifests, newest first.

    Only ``builds/<build_id>.json`` files whose manifest names that same
    build are accepted, so a stray copy, an editor's backup or a half-written
    file can neither appear in the list nor appear twice.
    """
    folder = os.path.join(root or builds_dir(), "builds")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    known = _build_index.get(folder) or {}
    index: Dict[str, Tuple[Tuple[int, int, int], Optional[int]]] = {}
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext != ".json" or not is_build_id(stem):
            continue
        signature = _signature(os.path.join(folder, name))
        if signature is None:
            continue
        entry = known.get(name)
        if entry is None or entry[0] != signature:
            signature, data = _parse_file(os.path.join(folder, name))
            if signature is None:
                continue
            manifest = _manifest(data)
            if manifest is None or manifest["build_id"] != stem:
                entry = (signature, None)
            else:
                entry = (signature, iso_to_unix(manifest.get("built_at")) or 0)
        index[name] = entry
    _build_index[folder] = index

    try:
        wanted = max(0, int(limit))
    except (TypeError, ValueError):
        wanted = 0
    newest_first = sorted(
        (name for name, entry in index.items() if entry[1] is not None),
        key=lambda name: (index[name][1], name),
        reverse=True,
    )
    builds: List[dict] = []
    for name in newest_first:
        if len(builds) >= wanted:
            break
        manifest = _manifest(_parse_file(os.path.join(folder, name))[1])
        if manifest is not None and manifest["build_id"] == name[:-len(".json")]:
            builds.append(manifest)
    return builds


def safe_zip_name(value: Any) -> Optional[str]:
    """``value`` if it is a bare zip file name that is safe to serve, else None."""
    if (isinstance(value, str) and len(value) <= MAX_ZIP_NAME_LENGTH
            and SAFE_ZIP_RE.fullmatch(value) is not None):
        return value
    return None


def zip_path(manifest: Optional[dict], root: Optional[str] = None) -> Optional[str]:
    """Absolute path of a manifest's zip, or None unless it can be served.

    That takes a safe name and a file that is really there: only the newest
    few zips are kept, so an older manifest outlives its zip.
    """
    name = safe_zip_name(manifest.get("file")) if isinstance(manifest, dict) else None
    if name is None:
        return None
    path = os.path.join(os.path.abspath(root or builds_dir()), "builds", name)
    return path if os.path.isfile(path) else None


# --------------------------------------------------------------------------- #
# API shapes
# --------------------------------------------------------------------------- #
def iso_to_unix(value: Any) -> Optional[int]:
    """An ISO-8601 time (``...Z`` or ``...+00:00``) as unix seconds, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            # The build job writes UTC. A time with no offset is one it wrote
            # carelessly, not one in this box's local time.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _str(value: Any) -> Optional[str]:
    """A non-empty string, or None. Bare numbers count ("6.0" typed unquoted)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _commit(value: Any) -> str:
    """A git commit id, or "". Callers build links from it."""
    text = _str(value)
    return text if text and _COMMIT_RE.fullmatch(text) else ""


def _short(value: Any, commit: str) -> str:
    return _commit(value) or commit[:7]


def _url(value: Any) -> Optional[str]:
    """An http(s) link, or None. It is rendered as a link, so no other scheme."""
    text = _str(value)
    if text and text.lower().startswith(("https://", "http://")):
        return text
    return None


def change_payload(change: Any) -> dict:
    """One change (commit) of a build, in the API's shape."""
    change = change if isinstance(change, dict) else {}
    commit = _commit(change.get("commit"))
    points = change.get("points")
    pr = _int(change.get("pr"))
    return {
        "commit": commit,
        "short": _short(change.get("short"), commit),
        "version": _str(change.get("version")),
        "title": _str(change.get("title")) or _str(change.get("subject")) or "",
        "points": ([text for text in map(_str, points) if text][:MAX_POINTS]
                   if isinstance(points, list) else []),
        "date": iso_to_unix(change.get("date")),
        "pr": pr if pr is not None and pr > 0 else None,
    }


def _changes(value: Any) -> List[dict]:
    if not isinstance(value, list):
        return []
    return [change_payload(change) for change in value if isinstance(change, dict)][:MAX_CHANGES]


def _release_payload(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    commit = _commit(value.get("commit"))
    if not commit:
        return None
    return {
        "commit": commit,
        "short": _short(value.get("short"), commit),
        "version": _str(value.get("version")),
        "is_ancestor": value.get("is_ancestor") is True,
    }


def build_payload(manifest: Any, include_since_release: bool = True) -> dict:
    """A build manifest in the API's shape.

    ``include_since_release=False`` leaves that list empty: it repeats most
    of every older build's changes, and only the current build's is shown.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    plugin = manifest.get("plugin") if isinstance(manifest.get("plugin"), dict) else {}
    commit = _commit(plugin.get("commit"))
    sha256 = _str(manifest.get("sha256"))
    return {
        "build_id": _str(manifest.get("build_id")) or "",
        "version": _str(plugin.get("version")) or "",
        "commit": commit,
        "short": _short(plugin.get("short"), commit),
        "ref": _str(plugin.get("ref")),
        "runelite_version": _str(manifest.get("runelite_version")) or "",
        "built_at": iso_to_unix(manifest.get("built_at")),
        "size_bytes": max(0, _int(manifest.get("size_bytes")) or 0),
        "sha256": sha256.lower() if sha256 and _SHA256_RE.fullmatch(sha256) else None,
        "reason": _str(manifest.get("reason")) or "",
        "repo_url": _url(manifest.get("repo_url")),
        "release": _release_payload(manifest.get("release")),
        "changes": _changes(manifest.get("changes")),
        "since_release": (_changes(manifest.get("since_release"))
                          if include_since_release else []),
    }


def build_summary(manifest: Any) -> dict:
    """The few fields a list of builds shows for each one."""
    manifest = manifest if isinstance(manifest, dict) else {}
    plugin = manifest.get("plugin") if isinstance(manifest.get("plugin"), dict) else {}
    return {
        "build_id": _str(manifest.get("build_id")) or "",
        "version": _str(plugin.get("version")),
        "runelite_version": _str(manifest.get("runelite_version")),
        "built_at": iso_to_unix(manifest.get("built_at")),
        "reason": _str(manifest.get("reason")),
    }


def status_payload(status: Any) -> dict:
    """The pipeline status in the API's shape; ``unknown`` when there is none."""
    status = status if isinstance(status, dict) else {}
    state = status.get("state")
    error = _str(status.get("error"))
    return {
        "state": state if isinstance(state, str) and state in _STATES else "unknown",
        "checked_at": iso_to_unix(status.get("checked_at")),
        "ref": _str(status.get("ref")),
        "head_commit": _commit(status.get("head_commit")) or None,
        "runelite_version": _str(status.get("runelite_version")),
        "error": error[:_MAX_ERROR_CHARS] if error else None,
        "current_build_id": _str(status.get("current_build_id")),
    }


def release_version(manifest: Any) -> Optional[str]:
    """The plugin version on the Plugin Hub when this build was made, if known."""
    release = manifest.get("release") if isinstance(manifest, dict) else None
    return _str(release.get("version")) if isinstance(release, dict) else None
