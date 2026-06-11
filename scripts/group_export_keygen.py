#!/usr/bin/env python3
"""
Generate and assign unique export API keys per group.

This script updates rows in `group_configurations` where:
  config_key = 'export_api_key'

For each row, it writes a deterministic hash to `config_value` based on:
  group_id + row_id + random salt + secret pepper

Usage examples:
  # Dry run (prints changes, no DB writes)
  python scripts/group_export_keygen.py --dry-run

  # Execute updates
  python scripts/group_export_keygen.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple

import pymysql


CONFIG_KEY = "export_api_key"
TABLE_NAME = "group_configurations"


@dataclass
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and store unique hashed export API keys per group."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview updates without writing to the database.",
    )
    parser.add_argument(
        "--only-zero-values",
        action="store_true",
        help="Only update rows where config_value is currently '0'.",
    )
    return parser.parse_args()


def load_db_config() -> DbConfig:
    missing = []

    return DbConfig(
        host="localhost",
        port=3306,
        user="root",
        password="TeethItNewsDistant4Met123",
        database="data",
    )


def build_key(group_id: int, row_id: int, pepper: str) -> str:
    # 24-byte random salt keeps each generated key unique.
    salt = secrets.token_hex(24)
    timestamp = datetime.now(timezone.utc).isoformat()
    raw = f"{group_id}:{row_id}:{salt}:{timestamp}:{pepper}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_target_rows(
    cursor: pymysql.cursors.Cursor, only_zero_values: bool
) -> List[Tuple[int, int, str]]:
    sql = (
        f"SELECT id, group_id, config_value "
        f"FROM {TABLE_NAME} "
        f"WHERE config_key = %s"
    )
    params: list[object] = [CONFIG_KEY]

    if only_zero_values:
        sql += " AND config_value = %s"
        params.append("0")

    sql += " ORDER BY id"
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def main() -> int:
    args = parse_args()
    pepper = "nJ8Rih8TU4MdkMyAMBSDhr2i"
    if not pepper:
        raise ValueError(
            "Missing required environment variable: EXPORT_API_KEY_PEPPER"
        )

    db = load_db_config()
    connection = pymysql.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        database=db.database,
        charset=db.charset,
        autocommit=False,
    )

    try:
        with connection.cursor() as cursor:
            rows = fetch_target_rows(cursor, args.only_zero_values)
            if not rows:
                print("No rows matched. Nothing to update.")
                return 0

            print(f"Found {len(rows)} rows to process.")
            updates: List[Tuple[str, int]] = []

            for row_id, group_id, current_value in rows:
                new_key = build_key(group_id=group_id, row_id=row_id, pepper=pepper)
                updates.append((new_key, row_id))
                print(
                    f"id={row_id} group_id={group_id} "
                    f"old={current_value!r} new={new_key}"
                )

            if args.dry_run:
                print("Dry run complete. No database changes were made.")
                connection.rollback()
                return 0

            update_sql = (
                f"UPDATE {TABLE_NAME} "
                f"SET config_value = %s, updated_at = NOW() "
                f"WHERE id = %s"
            )
            cursor.executemany(update_sql, updates)
            connection.commit()
            print(f"Committed {cursor.rowcount} updates.")
            return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())