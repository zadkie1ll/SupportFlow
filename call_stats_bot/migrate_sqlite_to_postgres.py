from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from call_stats_bot.store import CallStore
from call_stats_bot.substitutions import SubstitutionStore


TABLES = (
    "calls",
    "call_analyses",
    "daily_reports",
    "manager_daily_analyses",
    "substitutions",
    "substitution_deals",
    "telegram_wizards",
)


def _sqlite_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")]


def _rows_digest(rows: Iterable[dict[str, Any]], columns: list[str]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = [row.get(column) for column in columns]
        digest.update(
            json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _source_rows(
    db: sqlite3.Connection, table: str, columns: list[str]
) -> list[dict[str, Any]]:
    selected = ",".join(f'"{column}"' for column in columns)
    ordered = ",".join(f'"{column}"' for column in columns)
    return [dict(row) for row in db.execute(
        f'SELECT {selected} FROM "{table}" ORDER BY {ordered}'
    ).fetchall()]


def _destination_rows(
    db: psycopg.Connection, table: str, columns: list[str]
) -> list[dict[str, Any]]:
    selected = ",".join(f'"{column}"' for column in columns)
    ordered = ",".join(f'"{column}"' for column in columns)
    return list(db.execute(
        f'SELECT {selected} FROM "{table}" ORDER BY {ordered}'
    ).fetchall())


def migrate(sqlite_path: Path, database_url: str) -> dict[str, dict[str, Any]]:
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)

    # Initialize the exact schema used by the application before copying data.
    CallStore(database_url)
    SubstitutionStore(database_url)

    source = sqlite3.connect(
        sqlite_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
    )
    source.row_factory = sqlite3.Row
    result: dict[str, dict[str, Any]] = {}
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as destination:
            nonempty = {
                table: int(destination.execute(
                    f'SELECT COUNT(*) AS amount FROM "{table}"'
                ).fetchone()["amount"])
                for table in TABLES
            }
            occupied = {table: count for table, count in nonempty.items() if count}
            if occupied:
                raise RuntimeError(f"Destination is not empty: {occupied}")

            for table in TABLES:
                columns = _sqlite_columns(source, table)
                if not columns:
                    raise RuntimeError(f"Source table is missing: {table}")
                rows = _source_rows(source, table, columns)
                if rows:
                    names = ",".join(f'"{column}"' for column in columns)
                    placeholders = ",".join("%s" for _ in columns)
                    with destination.cursor() as cursor:
                        cursor.executemany(
                            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                            [tuple(row[column] for column in columns) for row in rows],
                        )

            # Validate the whole row content, not only counts, before committing.
            for table in TABLES:
                columns = _sqlite_columns(source, table)
                source_rows = _source_rows(source, table, columns)
                destination_rows = _destination_rows(destination, table, columns)
                source_hash = _rows_digest(source_rows, columns)
                destination_hash = _rows_digest(destination_rows, columns)
                if len(source_rows) != len(destination_rows) or source_hash != destination_hash:
                    raise RuntimeError(
                        f"Validation failed for {table}: "
                        f"source={len(source_rows)}/{source_hash}, "
                        f"destination={len(destination_rows)}/{destination_hash}"
                    )
                result[table] = {
                    "rows": len(source_rows),
                    "sha256": source_hash,
                }
    finally:
        source.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy and verify the complete SupportFlow SQLite database in PostgreSQL"
    )
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.getenv("CALLBOT_DATABASE_URL", ""),
        help="PostgreSQL DSN; defaults to CALLBOT_DATABASE_URL",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or CALLBOT_DATABASE_URL is required")
    print(json.dumps(migrate(args.sqlite, args.database_url), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
