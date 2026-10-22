from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class EmployeeStats:
    calls: int
    duration_seconds: int


class CallStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
                    status TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def save_call(
        self,
        *,
        call_id: str,
        employee_id: str,
        started_at: datetime,
        duration_seconds: int,
        status: str = "",
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO calls(call_id, employee_id, started_at, duration_seconds, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    employee_id = excluded.employee_id,
                    started_at = excluded.started_at,
                    duration_seconds = excluded.duration_seconds,
                    status = excluded.status
                """,
                (
                    call_id,
                    employee_id,
                    started_at.astimezone(utc_timezone.utc).isoformat(),
                    duration_seconds,
                    status,
                ),
            )
        return cursor.rowcount > 0

    def daily_stats(
        self, day: date, timezone: str, employee_ids: list[str]
    ) -> dict[str, EmployeeStats]:
        result = {employee_id: EmployeeStats(0, 0) for employee_id in employee_ids}
        if not employee_ids:
            return result

        zone = ZoneInfo(timezone)
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(utc_timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            utc_timezone.utc
        )
        placeholders = ",".join("?" for _ in employee_ids)
        query = f"""
            SELECT employee_id, COUNT(*) AS calls, SUM(duration_seconds) AS duration
            FROM calls
            WHERE started_at >= ? AND started_at < ?
              AND employee_id IN ({placeholders})
            GROUP BY employee_id
        """
        with self._connect() as db:
            rows = db.execute(
                query, [start.isoformat(), end.isoformat(), *employee_ids]
            ).fetchall()
        for row in rows:
            result[row["employee_id"]] = EmployeeStats(
                int(row["calls"]), int(row["duration"] or 0)
            )
        return result
