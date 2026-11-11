"""Retention for ``player_stat_snapshots``: keep recent history whole, older
history at one row per player per day, and lose nothing on the way.

Three jobs, run nightly by ``scripts.stat_snapshot_retention``:

1. **Partitions.** The table is partitioned by month with a ``pmax``
   catch-all. New months are split out of ``pmax`` ahead of time so it stays
   empty.
2. **Archive, then downsample.** A day older than ``HOT_DAYS`` is written to
   B2 in full, read back and checked, and only then thinned to the rows
   `rows_to_delete` spares. A failed or unverifiable upload deletes nothing.
3. **Rebuild.** When the last day of a month has been thinned, its partition
   is rebuilt to hand the space back.

Everything that touches the outside world (object storage, the progress
marker) is passed in, so the rules are testable without either.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

TABLE = "player_stat_snapshots"
HOT_DAYS = 90
MONTHS_AHEAD = 6
# The B2 key is locked to names starting ``dt_``; a put outside it looks like
# a network fault rather than a refusal.
ARCHIVE_PREFIX = "dt_stats/snapshots"
WATERMARK_KEY = "stat_snapshots:downsampled_through"
COLUMNS = ("player_id", "taken_at", "source", "account_type", "total_xp", "total_level",
           "skills", "activities", "ranks", "ehp", "ehb", "flagged", "pinned")

Key = Tuple[int, str, str]


class ArchiveError(RuntimeError):
    """The archive could not be written or did not read back whole."""


# --------------------------------------------------------------------------- #
# Partitions
# --------------------------------------------------------------------------- #

def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def partition_name(d: date) -> str:
    return f"p{d.year:04d}{d.month:02d}"


def missing_partitions(existing: Iterable[str], today: date,
                       ahead: int = MONTHS_AHEAD) -> List[Tuple[str, str]]:
    """Month partitions to split out of ``pmax``: ``[(name, less_than)]``.

    Covers every month from the one after the newest named partition through
    ``ahead`` months past ``today``. Empty for a table that is not
    partitioned this way.
    """
    names = set(existing)
    months = sorted(n for n in names if len(n) == 7 and n[0] == "p" and n[1:].isdigit())
    if "pmax" not in names or not months:
        return []
    newest = date(int(months[-1][1:5]), int(months[-1][5:7]), 1)
    last_wanted = today.replace(day=1)
    for _ in range(ahead):
        last_wanted = _next_month(last_wanted)
    out = []
    month = _next_month(newest)
    while month <= last_wanted:
        out.append((partition_name(month), f"{_next_month(month).isoformat()} 00:00:00"))
        month = _next_month(month)
    return out


def partition_names(db_session) -> List[str]:
    from sqlalchemy import text

    if db_session.get_bind().dialect.name not in ("mysql", "mariadb"):
        return []
    rows = db_session.execute(text(
        "SELECT partition_name FROM information_schema.partitions "
        "WHERE table_schema = DATABASE() AND table_name = :t AND partition_name IS NOT NULL"
    ), {"t": TABLE}).fetchall()
    return [r[0] for r in rows]


def ensure_partitions(db_session, today: date, *, apply: bool) -> List[str]:
    """Split upcoming months out of ``pmax``. Returns the names (to be) added."""
    from sqlalchemy import text

    wanted = missing_partitions(partition_names(db_session), today)
    if wanted and apply:
        parts = ", ".join(f"PARTITION {name} VALUES LESS THAN ('{bound}')" for name, bound in wanted)
        db_session.execute(text(
            f"ALTER TABLE {TABLE} REORGANIZE PARTITION pmax INTO "
            f"({parts}, PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        ))
        db_session.commit()
    return [name for name, _ in wanted]


# --------------------------------------------------------------------------- #
# What survives downsampling
# --------------------------------------------------------------------------- #

def row_key(row: Dict[str, Any]) -> Key:
    taken_at = row["taken_at"]
    if isinstance(taken_at, datetime):
        taken_at = taken_at.strftime("%Y-%m-%d %H:%M:%S")
    return (int(row["player_id"]), str(taken_at), str(row["source"]))


def rows_to_delete(rows: Sequence[Dict[str, Any]]) -> List[Key]:
    """Keys of one day's rows that downsampling removes.

    Spared: every pinned row, and each player's last usable (unflagged) row
    of the day, which is the day's closing values. Everything else goes,
    including flagged rows, which no reader uses.
    """
    last: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        if row.get("flagged"):
            continue
        held = last.get(int(row["player_id"]))
        if held is None or row_key(row)[1:] > row_key(held)[1:]:
            last[int(row["player_id"])] = row
    spared = {row_key(row) for row in last.values()}
    return [row_key(row) for row in rows if not row.get("pinned") and row_key(row) not in spared]


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #

def archive_key(day: date, secret: str = "") -> str:
    """Object key for a day's archive.

    The bucket is served publicly by the CDN, key for key, so a name built
    from the date alone could be fetched by anyone who guessed it. With a
    ``secret`` the name carries a keyed hash of the day: stable across
    reruns, not guessable without the secret.
    """
    name = day.isoformat()
    if secret:
        digest = hmac.new(secret.encode("utf-8"), name.encode("utf-8"), hashlib.sha256)
        name = f"{name}-{digest.hexdigest()[:24]}"
    return f"{ARCHIVE_PREFIX}/{day.year:04d}/{day.month:02d}/{name}.jsonl.gz"


def _plain(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {column: row.get(column) for column in COLUMNS}
    out["taken_at"] = row_key(row)[1]
    out["flagged"] = bool(out["flagged"])
    out["pinned"] = bool(out["pinned"])
    return out


def serialize_rows(rows: Iterable[Dict[str, Any]]) -> bytes:
    """Gzipped JSON lines, one row each, in key order (so reruns are stable)."""
    plain = sorted((_plain(row) for row in rows), key=row_key)
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in plain)
    return gzip.compress(body.encode("utf-8"), mtime=0)


def parse_archive(data: bytes) -> List[Dict[str, Any]]:
    text_ = gzip.decompress(data).decode("utf-8")
    return [json.loads(line) for line in text_.splitlines() if line.strip()]


def archive_day(day: date, rows: Sequence[Dict[str, Any]], doomed: Sequence[Key], *,
                put: Callable[[str, bytes], Any],
                get: Callable[[str], Optional[bytes]], secret: str = "") -> int:
    """Write a day's rows to the archive and prove the doomed ones are in it.

    An archive that already exists (an earlier run died between upload and
    delete) is merged with, never replaced by, what the table holds now: by
    then the table may hold less. Raises `ArchiveError` unless every doomed
    row reads back. Returns the number of rows in the archive.
    """
    key = archive_key(day, secret)
    try:
        previous = get(key)
        merged = {row_key(row): row for row in (parse_archive(previous) if previous else [])}
        merged.update({row_key(row): _plain(row) for row in rows})
        put(key, serialize_rows(merged.values()))
        stored = get(key)
        if not stored:
            raise ArchiveError(f"{key}: not there after upload")
        archived = {row_key(row) for row in parse_archive(stored)}
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError(f"{key}: {type(exc).__name__}: {exc}") from exc
    lost = [k for k in doomed if k not in archived]
    if lost:
        raise ArchiveError(f"{key}: {len(lost)} rows due for deletion are not in the archive")
    return len(archived)


# --------------------------------------------------------------------------- #
# Downsample
# --------------------------------------------------------------------------- #

def day_rows(db_session, day: date) -> List[Dict[str, Any]]:
    from db.models import PlayerStatSnapshot

    start = datetime.combine(day, datetime.min.time())
    found = (
        db_session.query(PlayerStatSnapshot)
        .filter(PlayerStatSnapshot.taken_at >= start,
                PlayerStatSnapshot.taken_at < start + timedelta(days=1))
        .all()
    )
    return [{column: getattr(row, column) for column in COLUMNS} for row in found]


def delete_rows(db_session, doomed: Sequence[Key]) -> int:
    """Delete by key, one statement per player and source, a commit each."""
    from db.models import PlayerStatSnapshot

    grouped: Dict[Tuple[int, str], List[datetime]] = {}
    for player_id, taken_at, source in doomed:
        grouped.setdefault((player_id, source), []).append(
            datetime.strptime(taken_at, "%Y-%m-%d %H:%M:%S"))
    deleted = 0
    for (player_id, source), times in grouped.items():
        deleted += (
            db_session.query(PlayerStatSnapshot)
            .filter(PlayerStatSnapshot.player_id == player_id,
                    PlayerStatSnapshot.source == source,
                    PlayerStatSnapshot.taken_at.in_(times),
                    PlayerStatSnapshot.pinned.is_(False))
            .delete(synchronize_session=False)
        )
        db_session.commit()
    return deleted


def downsample_day(db_session, day: date, *, apply: bool,
                   put: Callable[[str, bytes], Any],
                   get: Callable[[str], Optional[bytes]], secret: str = "") -> Dict[str, int]:
    """Archive one day and thin it. Nothing is deleted unless the archive holds."""
    rows = day_rows(db_session, day)
    doomed = rows_to_delete(rows)
    stats = {"rows": len(rows), "doomed": len(doomed), "archived": 0, "deleted": 0}
    if not doomed or not apply:
        return stats
    stats["archived"] = archive_day(day, rows, doomed, put=put, get=get, secret=secret)
    stats["deleted"] = delete_rows(db_session, doomed)
    return stats


def first_day(db_session) -> Optional[date]:
    from sqlalchemy import func

    from db.models import PlayerStatSnapshot

    oldest = db_session.query(func.min(PlayerStatSnapshot.taken_at)).scalar()
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    return oldest.date() if oldest else None


def rebuild_partition(db_session, month: date) -> bool:
    from sqlalchemy import text

    name = partition_name(month)
    if name not in partition_names(db_session):
        return False
    db_session.execute(text(f"ALTER TABLE {TABLE} REBUILD PARTITION {name}"))
    db_session.commit()
    return True


def run(db_session, today: date, *, apply: bool, max_days: int,
        put: Callable[[str, bytes], Any], get: Callable[[str], Optional[bytes]],
        read_watermark: Callable[[], Optional[str]],
        write_watermark: Callable[[str], Any],
        log: Callable[[str], Any] = print, secret: str = "") -> Dict[str, Any]:
    """One retention pass. Stops at the first day whose archive fails."""
    report: Dict[str, Any] = {"partitions_added": ensure_partitions(db_session, today, apply=apply),
                              "days": {}, "rebuilt": []}
    if report["partitions_added"]:
        log(f"partitions {'added' if apply else 'to add'}: {', '.join(report['partitions_added'])}")

    # Days strictly older than the hot window, oldest first.
    last = today - timedelta(days=HOT_DAYS + 1)
    mark = None
    try:
        raw = read_watermark()
        mark = date.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
    except Exception:
        mark = None
    day = mark + timedelta(days=1) if mark else first_day(db_session)
    done = 0
    while day is not None and day <= last and done < max_days:
        stats = downsample_day(db_session, day, apply=apply, put=put, get=get, secret=secret)
        report["days"][day.isoformat()] = stats
        log(f"{day}: {stats['rows']} rows, {stats['doomed']} to thin, "
            f"{stats['archived']} archived, {stats['deleted']} deleted")
        if apply:
            write_watermark(day.isoformat())
            if stats["deleted"] and (day + timedelta(days=1)).day == 1:
                if rebuild_partition(db_session, day):
                    report["rebuilt"].append(partition_name(day))
                    log(f"rebuilt {partition_name(day)}")
        day += timedelta(days=1)
        done += 1
    return report
