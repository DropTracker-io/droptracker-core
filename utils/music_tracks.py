"""Music track unlocks from raw varps (suggestion #252).

The plugin sends the music unlock varps the manifest lists (``music_varps``)
as raw 32-bit values, and this decodes them against the track registry
extracted from the game cache (``scripts/music_tracks.json``, served to the
server only as the ``music_tracks`` manifest section). The same split as the
combat achievement varps: a registry fix applies to every stored sync.

Stdlib-only, so unit tests import it directly.
"""
from __future__ import annotations

from typing import Iterable, Mapping


def decode(varps: Mapping[int, int], tracks: Iterable[Mapping]) -> dict:
    """``{"unlocked": n, "total": n, "unlocked_ids": [...]}``.

    A track counts when its bit is set or the game unlocks it automatically.
    Hidden tracks are not part of the in-game list and are skipped. A track
    whose varp the client did not send is counted in the total but never as
    unlocked, so a partial sync understates rather than invents.
    """
    unlocked_ids = []
    total = 0
    for track in tracks:
        if track.get("hidden"):
            continue
        total += 1
        if track.get("automatic"):
            unlocked_ids.append(int(track["id"]))
            continue
        value = varps.get(int(track["varp"]))
        if value is None:
            continue
        if (int(value) & 0xFFFFFFFF) >> int(track["bit"]) & 1:
            unlocked_ids.append(int(track["id"]))
    return {"unlocked": len(unlocked_ids), "total": total, "unlocked_ids": unlocked_ids}


_REGISTRY_TTL_SECONDS = 300
_registry_cache: dict = {}


def load_registry(session) -> list:
    """The track list: the ``music_tracks`` manifest section, or the extracted
    file beside the build script until that section has been built. Cached for
    a few minutes per process."""
    import json
    import os
    import time

    now = time.monotonic()
    cached = _registry_cache.get("tracks")
    if cached is not None and cached[0] > now:
        return cached[1]

    tracks = []
    try:
        from db.models import PluginManifestSection

        row = (
            session.query(PluginManifestSection)
            .filter(PluginManifestSection.key == "music_tracks")
            .first()
        )
        if row is not None:
            payload = json.loads(row.payload)
            if isinstance(payload, dict):
                tracks = payload.get("tracks") or []
    except Exception:
        tracks = []
    if not tracks:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "music_tracks.json",
        )
        try:
            with open(path) as fh:
                tracks = json.load(fh).get("tracks") or []
        except (OSError, ValueError):
            tracks = []
    if tracks:
        _registry_cache["tracks"] = (now + _REGISTRY_TTL_SECONDS, tracks)
    return tracks
