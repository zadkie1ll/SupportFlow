from __future__ import annotations

import json
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
        Path(path).chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
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
                    status TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    client_phone TEXT NOT NULL DEFAULT '',
                    company_phone TEXT NOT NULL DEFAULT '',
                    recording_url TEXT NOT NULL DEFAULT '',
                    ats_user TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(calls)").fetchall()
            }
            for name in (
                "direction", "client_phone", "company_phone", "recording_url", "ats_user"
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE calls ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS call_analyses (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','processing','completed','error','skipped')),
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_segments_json TEXT NOT NULL DEFAULT '[]',
                    summary_text TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    transcription_model TEXT NOT NULL DEFAULT '',
                    summary_model TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_call_analyses_status
                    ON call_analyses(status, updated_at);
                CREATE TABLE IF NOT EXISTS daily_reports (
                    report_day TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','processing','completed','attention')),
                    employee_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    stats_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    total_calls INTEGER NOT NULL DEFAULT 0,
                    analyzable_calls INTEGER NOT NULL DEFAULT 0,
                    completed_calls INTEGER NOT NULL DEFAULT 0,
                    failed_calls INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            report_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(daily_reports)").fetchall()
            }
            if "last_error" not in report_columns:
                db.execute(
                    "ALTER TABLE daily_reports ADD COLUMN last_error TEXT NOT NULL DEFAULT ''"
                )

    def save_call(
        self,
        *,
        call_id: str,
        employee_id: str,
        started_at: datetime,
        duration_seconds: int,
        status: str = "",
        direction: str = "",
        client_phone: str = "",
        company_phone: str = "",
        recording_url: str = "",
        ats_user: str = "",
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO calls(
                    call_id, employee_id, started_at, duration_seconds, status,
                    direction, client_phone, company_phone, recording_url, ats_user
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    employee_id = excluded.employee_id,
                    started_at = excluded.started_at,
                    duration_seconds = excluded.duration_seconds,
                    status = excluded.status,
                    direction = CASE WHEN excluded.direction <> ''
                        THEN excluded.direction ELSE calls.direction END,
                    client_phone = CASE WHEN excluded.client_phone <> ''
                        THEN excluded.client_phone ELSE calls.client_phone END,
                    company_phone = CASE WHEN excluded.company_phone <> ''
                        THEN excluded.company_phone ELSE calls.company_phone END,
                    recording_url = CASE WHEN excluded.recording_url <> ''
                        THEN excluded.recording_url ELSE calls.recording_url END,
                    ats_user = CASE WHEN excluded.ats_user <> ''
                        THEN excluded.ats_user ELSE calls.ats_user END
                """,
                (
                    call_id,
                    employee_id,
                    started_at.astimezone(utc_timezone.utc).isoformat(),
                    duration_seconds,
                    status,
                    direction,
                    client_phone,
                    company_phone,
                    recording_url,
                    ats_user,
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

    @staticmethod
    def _now() -> str:
        return datetime.now(utc_timezone.utc).isoformat()

    @staticmethod
    def _bounds(day: date, timezone: str) -> tuple[str, str]:
        zone = ZoneInfo(timezone)
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(utc_timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            utc_timezone.utc
        )
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _decode_json(value: str, fallback: object) -> object:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback

    def calls_for_day(
        self, day: date, timezone: str, employee_id: str = "",
        *, include_content: bool = False,
    ) -> list[dict]:
        start, end = self._bounds(day, timezone)
        filters = ["c.started_at >= ?", "c.started_at < ?"]
        params: list[object] = [start, end]
        if employee_id:
            filters.append("c.employee_id = ?")
            params.append(employee_id)
        query = f"""
            SELECT c.*, a.status AS analysis_status, a.summary_text,
                   a.summary_json, a.transcript_text, a.transcript_segments_json,
                   a.last_error AS analysis_error, a.attempts AS analysis_attempts,
                   a.completed_at AS analysis_completed_at
            FROM calls c
            LEFT JOIN call_analyses a ON a.call_id = c.call_id
            WHERE {' AND '.join(filters)}
            ORDER BY c.started_at DESC, c.call_id
        """
        zone = ZoneInfo(timezone)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["started_at"] = datetime.fromisoformat(item["started_at"]).astimezone(
                zone
            ).isoformat()
            item["summary"] = self._decode_json(item.pop("summary_json") or "{}", {})
            item["segments"] = self._decode_json(
                item.pop("transcript_segments_json") or "[]", []
            )
            if not include_content:
                item.pop("transcript_text", None)
                item.pop("segments", None)
            result.append(item)
        return result

    def call_detail(self, call_id: str, timezone: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT c.*, a.status AS analysis_status, a.summary_text,
                       a.summary_json, a.transcript_text, a.transcript_segments_json,
                       a.last_error AS analysis_error, a.attempts AS analysis_attempts,
                       a.completed_at AS analysis_completed_at
                FROM calls c LEFT JOIN call_analyses a ON a.call_id=c.call_id
                WHERE c.call_id=?
                """,
                (call_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["started_at"] = datetime.fromisoformat(item["started_at"]).astimezone(
            ZoneInfo(timezone)
        ).isoformat()
        item["summary"] = self._decode_json(item.pop("summary_json") or "{}", {})
        item["segments"] = self._decode_json(
            item.pop("transcript_segments_json") or "[]", []
        )
        return item

    def begin_daily_report(
        self, day: date, timezone: str, employees: dict[str, str]
    ) -> None:
        stats = self.daily_stats(day, timezone, list(employees))
        stats_payload = {
            employee_id: {
                "name": employees[employee_id],
                "calls": item.calls,
                "duration_seconds": item.duration_seconds,
            }
            for employee_id, item in stats.items()
        }
        calls = self.calls_for_day(day, timezone)
        analyzable = sum(bool(item["recording_url"]) for item in calls)
        start, end = self._bounds(day, timezone)
        now = self._now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE call_analyses SET status='pending', updated_at=?
                WHERE status='processing' AND call_id IN (
                    SELECT call_id FROM calls WHERE started_at >= ? AND started_at < ?
                )
                """,
                (now, start, end),
            )
            db.execute(
                """
                INSERT INTO daily_reports(
                    report_day, status, employee_snapshot_json, stats_snapshot_json,
                    total_calls, analyzable_calls, created_at, started_at, updated_at
                ) VALUES (?, 'processing', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_day) DO UPDATE SET
                    status='processing', employee_snapshot_json=excluded.employee_snapshot_json,
                    stats_snapshot_json=excluded.stats_snapshot_json,
                    total_calls=excluded.total_calls,
                    analyzable_calls=excluded.analyzable_calls,
                    started_at=excluded.started_at, completed_at=NULL,
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (
                    day.isoformat(), json.dumps(employees, ensure_ascii=False),
                    json.dumps(stats_payload, ensure_ascii=False), len(calls), analyzable,
                    now, now, now,
                ),
            )

    def analysis_candidates(
        self, day: date, timezone: str, *, retry_errors: bool = True
    ) -> list[dict]:
        start, end = self._bounds(day, timezone)
        statuses = "('pending','error')" if retry_errors else "('pending')"
        now = self._now()
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO call_analyses(call_id, status, updated_at)
                SELECT call_id, CASE WHEN recording_url='' THEN 'skipped' ELSE 'pending' END, ?
                FROM calls WHERE started_at >= ? AND started_at < ?
                """,
                (now, start, end),
            )
            db.execute(
                """
                UPDATE call_analyses SET status='pending', updated_at=?
                WHERE status='skipped' AND call_id IN (
                    SELECT call_id FROM calls
                    WHERE started_at >= ? AND started_at < ? AND recording_url <> ''
                )
                """,
                (now, start, end),
            )
            rows = db.execute(
                f"""
                SELECT c.* FROM calls c JOIN call_analyses a ON a.call_id=c.call_id
                WHERE c.started_at >= ? AND c.started_at < ?
                  AND c.recording_url <> '' AND a.status IN {statuses} AND a.attempts < 3
                ORDER BY c.started_at, c.call_id
                """,
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_analysis_started(self, call_id: str) -> None:
        now = self._now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE call_analyses SET status='processing', attempts=attempts+1,
                    started_at=?, updated_at=?, last_error=''
                WHERE call_id=?
                """,
                (now, now, call_id),
            )

    def save_analysis(
        self, call_id: str, *, transcript_text: str, segments: list[dict],
        summary: dict, transcription_model: str, summary_model: str,
    ) -> None:
        now = self._now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE call_analyses SET status='completed', transcript_text=?,
                    transcript_segments_json=?, summary_text=?, summary_json=?,
                    transcription_model=?, summary_model=?, last_error='',
                    completed_at=?, updated_at=? WHERE call_id=?
                """,
                (
                    transcript_text, json.dumps(segments, ensure_ascii=False),
                    str(summary.get("short_summary") or ""),
                    json.dumps(summary, ensure_ascii=False), transcription_model,
                    summary_model, now, now, call_id,
                ),
            )

    def fail_analysis(self, call_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE call_analyses SET status='error', last_error=?, updated_at=?
                WHERE call_id=?
                """,
                (error[:1000], self._now(), call_id),
            )

    def set_daily_report_result(
        self, day: date, timezone: str
    ) -> dict[str, int | str]:
        start, end = self._bounds(day, timezone)
        with self._connect() as db:
            counts = {
                row["status"]: int(row["amount"])
                for row in db.execute(
                    """
                    SELECT a.status, COUNT(*) AS amount
                    FROM call_analyses a JOIN calls c ON c.call_id=a.call_id
                    WHERE c.started_at >= ? AND c.started_at < ?
                    GROUP BY a.status
                    """,
                    (start, end),
                ).fetchall()
            }
            completed = counts.get("completed", 0)
            failed = counts.get("error", 0)
            status = "attention" if failed else "completed"
            now = self._now()
            db.execute(
                """
                UPDATE daily_reports SET status=?, completed_calls=?, failed_calls=?,
                    completed_at=?, updated_at=? WHERE report_day=?
                """,
                (status, completed, failed, now, now, day.isoformat()),
            )
        return {"status": status, "completed": completed, "failed": failed,
                "skipped": counts.get("skipped", 0)}

    def mark_daily_report_attention(self, day: date, error: str) -> None:
        now = self._now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE daily_reports SET status='attention', last_error=?,
                    completed_at=?, updated_at=? WHERE report_day=?
                """,
                (error[:1000], now, now, day.isoformat()),
            )

    def recent_reports(self, limit: int = 31) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM daily_reports ORDER BY report_day DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["employees"] = self._decode_json(
                item.pop("employee_snapshot_json") or "{}", {}
            )
            item["stats"] = self._decode_json(
                item.pop("stats_snapshot_json") or "{}", {}
            )
            result.append(item)
        return result

    def report_detail(self, day: date, timezone: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM daily_reports WHERE report_day=?", (day.isoformat(),)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        employees = self._decode_json(result.pop("employee_snapshot_json") or "{}", {})
        stats = self._decode_json(result.pop("stats_snapshot_json") or "{}", {})
        calls = self.calls_for_day(day, timezone)
        for extension, name in employees.items():
            employee_calls = [item for item in calls if item["employee_id"] == extension]
            item = stats.setdefault(extension, {"name": name, "calls": 0, "duration_seconds": 0})
            item["analysis_completed"] = sum(
                call["analysis_status"] == "completed" for call in employee_calls
            )
            item["analysis_errors"] = sum(
                call["analysis_status"] == "error" for call in employee_calls
            )
            item["recordings"] = sum(bool(call["recording_url"]) for call in employee_calls)
        result["employees"] = stats
        return result
