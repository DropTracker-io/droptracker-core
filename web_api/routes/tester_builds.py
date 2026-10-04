"""Tester client builds: the Bug Tester download and the staff view of it.

  GET  /api/v1/tester-builds           -> the current build, older ones, my downloads
  POST /api/v1/tester-builds/download  -> record a download, name the file to serve
  GET  /api/v1/admin/tester-builds     -> pipeline status, builds, tester activity

The builds are files a private build job publishes to disk; everything read
from them goes through utils/tester_builds.py, which never trusts what it
finds. This module never sends a zip. The POST answers with a checked file
name, and the website's /dl/bugtest.zip route hands that name to nginx
(X-Accel-Redirect) to serve.

Downloads are for Bug Testers (an active ``bug_tester_helper`` badge on any
of the user's accounts) and for site developers. The staff view puts three
things side by side for each tester: the roster (services/tester_roster.py),
what they downloaded (``plugin_test_downloads``) and which plugin versions
their accounts have submitted with (``player_plugin_versions``, written by
utils/plugin_versions.py). That is how staff see whether a tester who took a
build went on to run it.

Downloads are not audit-logged: ``plugin_test_downloads`` is the record.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

from quart import Blueprint, jsonify
from sqlalchemy import func

from db.models import PlayerPluginVersion, PluginTestDownload, User
from utils import tester_builds as tb
from utils.plugin_versions import version_tuple
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_developer,
    current_user_id,
    is_bug_tester,
    is_developer,
    load_user,
)

tester_builds_bp = Blueprint("v1_tester_builds", __name__)

_RECENT_BUILDS = 8
_MY_DOWNLOADS = 10
_ADMIN_BUILDS = 20
_ADMIN_DOWNLOADS = 50
# A resumed or ranged download asks for the file again, and every one of those
# requests comes back through POST /tester-builds/download. Inside this window
# they are the same download.
_REDOWNLOAD_WINDOW = timedelta(minutes=10)


def _ts(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _assert_tester(s, user_id: int) -> bool:
    """Abort 403 unless the user may download builds. Returns whether they
    are staff, who may without holding the badge."""
    staff = is_developer(load_user(s, user_id))
    if not staff and not is_bug_tester(s, user_id):
        abort_problem(
            403,
            "Forbidden",
            "This download is for Bug Testers.",
            extra={"code": "bug_tester_required"},
        )
    return staff


def _download_payload(row) -> dict:
    return {
        "build_id": row.build_id,
        "version": row.plugin_version or None,
        "downloaded_at": _ts(row.created_at),
    }


def _older_builds(current: Optional[dict]) -> list:
    """Manifests of the builds published before the current one, newest first."""
    builds = tb.list_builds(limit=_ADMIN_BUILDS)
    if current is None:
        return builds[:_RECENT_BUILDS]
    current_built = tb.iso_to_unix(current.get("built_at"))
    older = []
    for manifest in builds:
        if manifest["build_id"] == current["build_id"]:
            continue
        built = tb.iso_to_unix(manifest.get("built_at"))
        # A manifest newer than the current one is a build the job has not
        # (or no longer) made current. It is not an earlier build.
        if current_built is not None and built is not None and built > current_built:
            continue
        older.append(manifest)
    return older[:_RECENT_BUILDS]


@tester_builds_bp.get("/tester-builds")
async def tester_builds():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            staff = _assert_tester(s, user_id)
            current = tb.load_current()
            mine = (
                s.query(PluginTestDownload)
                .filter(PluginTestDownload.user_id == user_id)
                .order_by(PluginTestDownload.created_at.desc(), PluginTestDownload.id.desc())
                .limit(_MY_DOWNLOADS)
                .all()
            )
            has_current = current is not None and (
                s.query(PluginTestDownload.id)
                .filter(
                    PluginTestDownload.user_id == user_id,
                    PluginTestDownload.build_id == current["build_id"],
                )
                .first()
            ) is not None
            return {
                "current": tb.build_payload(current) if current is not None else None,
                "recent": [
                    tb.build_payload(manifest, include_since_release=False)
                    for manifest in _older_builds(current)
                ],
                "my_downloads": [_download_payload(row) for row in mine],
                "has_current": has_current,
                "is_staff": staff,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@tester_builds_bp.post("/tester-builds/download")
async def tester_build_download():
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            _assert_tester(s, user_id)
            current = tb.load_current()
            path = tb.zip_path(current)
            if path is None:
                # No manifest, a file name that is not safe to serve, or a zip
                # that is no longer on disk. All three mean nothing to send.
                abort_problem(
                    404,
                    "Not found",
                    "There is no tester build to download right now.",
                    extra={"code": "no_build"},
                )
            build = tb.build_payload(current, include_since_release=False)
            file_name = os.path.basename(path)

            # A download manager sends several ranged requests at once. Taking
            # the user's row first makes them queue, and the engine is READ
            # COMMITTED, so each one's check sees the row the one before it
            # just committed instead of all of them inserting.
            s.query(User.user_id).filter(User.user_id == user_id).with_for_update().first()
            now = datetime.now()
            repeat = (
                s.query(PluginTestDownload.id)
                .filter(
                    PluginTestDownload.user_id == user_id,
                    PluginTestDownload.build_id == build["build_id"],
                    PluginTestDownload.created_at >= now - _REDOWNLOAD_WINDOW,
                )
                .first()
            )
            if repeat is None:
                s.add(
                    PluginTestDownload(
                        user_id=user_id,
                        build_id=build["build_id"],
                        plugin_version=build["version"][:32] or None,
                        commit_sha=build["commit"][:40] or None,
                        runelite_version=build["runelite_version"][:32] or None,
                        file_name=file_name,
                        # Stamped here, not by the column default, so the
                        # window above is measured on one clock.
                        created_at=now,
                    )
                )
            s.commit()
            size = build["size_bytes"]
            if not size:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
            return {
                "file": file_name,
                "filename": file_name,
                "build_id": build["build_id"],
                "version": build["version"],
                "size_bytes": int(size),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


def _version_order(version: str):
    """Sort key putting the newest version last; unreadable ones first."""
    return (version_tuple(version) or (-1,), version)


def _testers_payload(s, roster: dict, current_id: Optional[str]) -> list:
    """Every active Bug Tester with what they downloaded and what they ran."""
    users = roster.get("users") or []
    user_ids = [int(u["user_id"]) for u in users]
    if not user_ids:
        return []

    players = {}       # user_id -> [{"player_id", "name"}]
    owner = {}         # player_id -> user_id
    player_names = {}  # player_id -> name
    for p in roster.get("players") or []:
        if p.get("user_id") is None:
            continue
        pid, uid = int(p["player_id"]), int(p["user_id"])
        name = str(p.get("player_name") or "")
        owner[pid] = uid
        player_names[pid] = name
        # The roster also carries account hashes. Those stay here.
        players.setdefault(uid, []).append({"player_id": pid, "name": name})

    counts, last_ids = {}, {}
    for uid, count, last_id in (
        s.query(
            PluginTestDownload.user_id,
            func.count(PluginTestDownload.id),
            func.max(PluginTestDownload.id),
        )
        .filter(PluginTestDownload.user_id.in_(user_ids))
        .group_by(PluginTestDownload.user_id)
        .all()
    ):
        counts[int(uid)] = int(count)
        last_ids[int(uid)] = int(last_id)
    last_rows = {
        int(row.user_id): row
        for row in s.query(PluginTestDownload)
        .filter(PluginTestDownload.id.in_(list(last_ids.values())))
        .all()
    } if last_ids else {}
    have_current = {
        int(uid)
        for (uid,) in s.query(PluginTestDownload.user_id)
        .filter(
            PluginTestDownload.build_id == current_id,
            PluginTestDownload.user_id.in_(user_ids),
        )
        .distinct()
        .all()
    } if current_id else set()

    sightings = {}  # user_id -> [PlayerPluginVersion]
    if owner:
        for row in (
            s.query(PlayerPluginVersion)
            .filter(PlayerPluginVersion.player_id.in_(list(owner)))
            .all()
        ):
            sightings.setdefault(owner[int(row.player_id)], []).append(row)

    testers = []
    for u in users:
        uid = int(u["user_id"])
        rows = sightings.get(uid) or []
        seen = None
        if rows:
            latest = max(rows, key=lambda r: (r.last_seen, _version_order(r.version)))
            seen = {
                "version": latest.version,
                "first_seen": _ts(latest.first_seen),
                "last_seen": _ts(latest.last_seen),
                "prerelease": bool(latest.prerelease),
                "player_name": player_names.get(int(latest.player_id)) or None,
            }
        last = last_rows.get(uid)
        testers.append({
            "user_id": uid,
            "discord_id": u.get("discord_id") or None,
            "username": u.get("username") or None,
            "players": players.get(uid, []),
            "download_count": counts.get(uid, 0),
            "last_download": _download_payload(last) if last is not None else None,
            "has_current": uid in have_current,
            "seen": seen,
            "tested_versions": sorted(
                {row.version for row in rows if row.prerelease},
                key=_version_order,
                reverse=True,
            ),
        })

    def _last_active(tester) -> int:
        seen_at = (tester["seen"] or {}).get("last_seen") or 0
        downloaded_at = (tester["last_download"] or {}).get("downloaded_at") or 0
        return max(seen_at, downloaded_at)

    # Most recently active first; the ones with nothing to show yet by name.
    testers.sort(key=lambda t: (t["username"] or "").lower())
    testers.sort(key=_last_active, reverse=True)
    return testers


@tester_builds_bp.get("/admin/tester-builds")
async def admin_tester_builds():
    user_id = current_user_id()

    def _load():
        from services.tester_roster import load_roster

        with db_session() as s:
            assert_developer(load_user(s, user_id))
            current = tb.load_current()
            current_id = current["build_id"] if current is not None else None

            manifests = tb.list_builds(limit=_ADMIN_BUILDS)
            build_ids = [manifest["build_id"] for manifest in manifests]
            totals = {
                build_id: (int(downloads), int(testers))
                for build_id, downloads, testers in (
                    s.query(
                        PluginTestDownload.build_id,
                        func.count(PluginTestDownload.id),
                        func.count(func.distinct(PluginTestDownload.user_id)),
                    )
                    .filter(PluginTestDownload.build_id.in_(build_ids))
                    .group_by(PluginTestDownload.build_id)
                    .all()
                )
            } if build_ids else {}
            builds = []
            for manifest in manifests:
                downloads, testers = totals.get(manifest["build_id"], (0, 0))
                builds.append(dict(tb.build_summary(manifest),
                                   downloads=downloads, testers=testers))

            recent = (
                s.query(PluginTestDownload, User.username)
                .outerjoin(User, User.user_id == PluginTestDownload.user_id)
                .order_by(PluginTestDownload.id.desc())
                .limit(_ADMIN_DOWNLOADS)
                .all()
            )
            return {
                "status": tb.status_payload(tb.load_status()),
                "current": tb.build_payload(current) if current is not None else None,
                "release_version": tb.release_version(current),
                "builds": builds,
                "testers": _testers_payload(s, load_roster(s), current_id),
                "recent_downloads": [
                    dict(_download_payload(row), user_id=int(row.user_id),
                         username=username or None)
                    for row, username in recent
                ],
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))
