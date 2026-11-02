from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


POSTGRES_PREFIXES = ("postgresql://", "postgres://")


def is_postgres(target: str) -> bool:
    return target.startswith(POSTGRES_PREFIXES)


def _postgres_placeholders(sql: str) -> str:
    """Convert the SQLite-style placeholders used by the stores to psycopg."""
    return sql.replace("?", "%s")


class DatabaseConnection:
    def __init__(self, target: str, *, timeout: int = 10):
        self.target = target
        self.postgres = is_postgres(target)
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            self.connection = psycopg.connect(
                target,
                row_factory=dict_row,
                connect_timeout=timeout,
            )
        else:
            connection = sqlite3.connect(target, timeout=timeout)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            self.connection = connection

    def __enter__(self) -> DatabaseConnection:
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return bool(self.connection.__exit__(exc_type, exc_value, traceback))

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        statement = _postgres_placeholders(sql) if self.postgres else sql
        return self.connection.execute(statement, tuple(params or ()))

    def executescript(self, script: str) -> None:
        if not self.postgres:
            self.connection.executescript(script)
            return
        for statement in script.split(";"):
            if statement.strip():
                self.connection.execute(statement)

    def columns(self, table: str) -> set[str]:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError("Unsafe table name")
        if self.postgres:
            rows = self.execute(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=?
                """,
                (table,),
            ).fetchall()
        else:
            rows = self.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}


class Database:
    def __init__(self, target: str):
        if not target:
            raise ValueError("Database target is required")
        self.target = target
        self.postgres = is_postgres(target)
        if not self.postgres:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, *, timeout: int = 10) -> DatabaseConnection:
        return DatabaseConnection(self.target, timeout=timeout)

    def secure_sqlite_file(self) -> None:
        if not self.postgres:
            Path(self.target).chmod(0o600)
