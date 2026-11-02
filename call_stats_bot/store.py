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
    missed_calls: int = 0


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
            if "period_start" not in report_columns:
                db.execute(
                    "ALTER TABLE daily_reports ADD COLUMN period_start TEXT NOT NULL DEFAULT ''"
                )
            if "period_end" not in report_columns:
                db.execute(
                    "ALTER TABLE daily_reports ADD COLUMN period_end TEXT NOT NULL DEFAULT ''"
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
        self, day: date, timezone: str, employee_ids: list[str],
        report_time: str | None = None,
    ) -> dict[str, EmployeeStats]:
        result = {employee_id: EmployeeStats(0, 0, 0) for employee_id in employee_ids}
        if not employee_ids:
            return result

        start, end = self._bounds(day, timezone, report_time)
        placeholders = ",".join("?" for _ in employee_ids)
        query = f"""
            SELECT c.employee_id, c.duration_seconds, c.status, c.recording_url,
                   a.summary_json
            FROM calls c LEFT JOIN call_analyses a ON a.call_id=c.call_id
            WHERE c.started_at >= ? AND c.started_at < ?
              AND c.employee_id IN ({placeholders})
        """
        with self._connect() as db:
            rows = db.execute(
                query, [start, end, *employee_ids]
            ).fetchall()
        totals = {
            employee_id: {"calls": 0, "duration": 0, "missed": 0}
            for employee_id in employee_ids
        }
        for row in rows:
            item = dict(row)
            item["summary"] = self._decode_json(item.pop("summary_json") or "{}", {})
            total = totals[row["employee_id"]]
            total["calls"] += 1
            total["duration"] += int(row["duration_seconds"] or 0)
            total["missed"] += int(self._is_missed_call(item))
        for employee_id, total in totals.items():
            result[employee_id] = EmployeeStats(
                total["calls"], total["duration"], total["missed"]
            )
        return result

    @staticmethod
    def _now() -> str:
        return datetime.now(utc_timezone.utc).isoformat()

    @staticmethod
    def _bounds(
        day: date, timezone: str, report_time: str | None = None,
    ) -> tuple[str, str]:
        zone = ZoneInfo(timezone)
        if report_time:
            hour, minute = (int(part) for part in report_time.split(":"))
            cutoff = time(hour, minute)
            end = datetime.combine(day, cutoff, tzinfo=zone).astimezone(utc_timezone.utc)
            start = datetime.combine(
                day - timedelta(days=1), cutoff, tzinfo=zone
            ).astimezone(utc_timezone.utc)
        else:
            start = datetime.combine(day, time.min, tzinfo=zone).astimezone(
                utc_timezone.utc
            )
            end = datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=zone
            ).astimezone(utc_timezone.utc)
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _decode_json(value: str, fallback: object) -> object:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _is_missed_call(item: dict) -> bool:
        summary = item.get("summary")
        is_voicemail = isinstance(summary, dict) and summary.get("is_voicemail") is True
        return is_voicemail or (
            not str(item.get("recording_url") or "").strip()
            and str(item.get("status") or "").strip().casefold()
            in {"missed", "busy", "notavailable"}
        )

    def calls_for_day(
        self, day: date, timezone: str, employee_id: str = "",
        *, include_content: bool = False, report_time: str | None = None,
    ) -> list[dict]:
        start, end = self._bounds(day, timezone, report_time)
        return self.calls_for_period(
            start, end, timezone, employee_id, include_content=include_content
        )

    def calls_for_period(
        self, start: str, end: str, timezone: str, employee_id: str = "",
        *, include_content: bool = False,
    ) -> list[dict]:
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
            item["is_missed"] = self._is_missed_call(item)
            item["is_voicemail"] = item["summary"].get("is_voicemail") is True
            result.append(item)
        # Keep conversations first and move non-connections to the bottom while
        # preserving newest-first order inside both groups.
        result.sort(key=lambda item: item["is_missed"])
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
        item["is_missed"] = self._is_missed_call(item)
        item["is_voicemail"] = item["summary"].get("is_voicemail") is True
        return item

    def begin_daily_report(
        self, day: date, timezone: str, employees: dict[str, str],
        report_time: str | None = None,
    ) -> None:
        stats = self.daily_stats(day, timezone, list(employees), report_time)
        stats_payload = {
            employee_id: {
                "name": employees[employee_id],
                "calls": item.calls,
                "duration_seconds": item.duration_seconds,
                "missed_calls": item.missed_calls,
            }
            for employee_id, item in stats.items()
        }
        calls = [
            call for call in self.calls_for_day(day, timezone, report_time=report_time)
            if call["employee_id"] in employees
        ]
        analyzable = sum(bool(item["recording_url"]) for item in calls)
        start, end = self._bounds(day, timezone, report_time)
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
                    total_calls, analyzable_calls, created_at, started_at, updated_at,
                    period_start, period_end
                ) VALUES (?, 'processing', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_day) DO UPDATE SET
                    status='processing', employee_snapshot_json=excluded.employee_snapshot_json,
                    stats_snapshot_json=excluded.stats_snapshot_json,
                    total_calls=excluded.total_calls,
                    analyzable_calls=excluded.analyzable_calls,
                    started_at=excluded.started_at, completed_at=NULL,
                    last_error='', period_start=excluded.period_start,
                    period_end=excluded.period_end,
                    updated_at=excluded.updated_at
                """,
                (
                    day.isoformat(), json.dumps(employees, ensure_ascii=False),
                    json.dumps(stats_payload, ensure_ascii=False), len(calls), analyzable,
                    now, now, now, start, end,
                ),
            )

    def analysis_candidates(
        self, day: date, timezone: str, *, retry_errors: bool = True,
        report_time: str | None = None,
    ) -> list[dict]:
        start, end = self._bounds(day, timezone, report_time)
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
            # A callback may arrive after the report boundary (for example, a call
            # started at 19:59 and finished after 20:00). Pick up every older
            # recording that has not been analyzed so it cannot fall between days.
            db.execute(
                """
                INSERT OR IGNORE INTO call_analyses(call_id, status, updated_at)
                SELECT call_id, 'pending', ? FROM calls
                WHERE started_at < ? AND recording_url <> ''
                """,
                (now, end),
            )
            db.execute(
                """
                UPDATE call_analyses SET status='pending', updated_at=?
                WHERE status='skipped' AND call_id IN (
                    SELECT call_id FROM calls
                    WHERE started_at < ? AND recording_url <> ''
                )
                """,
                (now, end),
            )
            rows = db.execute(
                f"""
                SELECT c.* FROM calls c JOIN call_analyses a ON a.call_id=c.call_id
                WHERE c.started_at < ? AND c.recording_url <> ''
                  AND a.status IN {statuses} AND a.attempts < 3
                ORDER BY c.started_at, c.call_id
                """,
                (end,),
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
        self, day: date, timezone: str, report_time: str | None = None,
    ) -> dict[str, int | str]:
        start, end = self._bounds(day, timezone, report_time)
        with self._connect() as db:
            report = db.execute(
                "SELECT employee_snapshot_json FROM daily_reports WHERE report_day=?",
                (day.isoformat(),),
            ).fetchone()
            employees = self._decode_json(
                report["employee_snapshot_json"] if report else "{}", {}
            )
            employee_ids = list(employees) if isinstance(employees, dict) else []
            placeholders = ",".join("?" for _ in employee_ids)
            employee_filter = (
                f" AND c.employee_id IN ({placeholders})" if employee_ids else " AND 0"
            )
            counts = {
                row["status"]: int(row["amount"])
                for row in db.execute(
                    f"""
                    SELECT a.status, COUNT(*) AS amount
                    FROM call_analyses a JOIN calls c ON c.call_id=a.call_id
                    WHERE c.started_at >= ? AND c.started_at < ? {employee_filter}
                    GROUP BY a.status
                    """,
                    (start, end, *employee_ids),
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

    def report_detail(
        self, day: date, timezone: str, employee_ids: set[str] | None = None,
    ) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM daily_reports WHERE report_day=?", (day.isoformat(),)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        employees = self._decode_json(result.pop("employee_snapshot_json") or "{}", {})
        stats = self._decode_json(result.pop("stats_snapshot_json") or "{}", {})
        if employee_ids is not None:
            employees = {
                extension: name for extension, name in employees.items()
                if extension in employee_ids
            }
            stats = {
                extension: item for extension, item in stats.items()
                if extension in employee_ids
            }
        start = str(result.get("period_start") or "")
        end = str(result.get("period_end") or "")
        if not start or not end:
            start, end = self._bounds(day, timezone)
        calls = [
            call for call in self.calls_for_period(start, end, timezone)
            if call["employee_id"] in employees
        ]
        for extension, name in employees.items():
            employee_calls = [item for item in calls if item["employee_id"] == extension]
            item = stats.setdefault(extension, {"name": name, "calls": 0, "duration_seconds": 0})
            item["missed_calls"] = sum(call["is_missed"] for call in employee_calls)
            item["analysis_completed"] = sum(
                call["analysis_status"] == "completed" for call in employee_calls
            )
            item["analysis_errors"] = sum(
                call["analysis_status"] == "error" for call in employee_calls
            )
            item["recordings"] = sum(bool(call["recording_url"]) for call in employee_calls)
        result["employees"] = stats
        result["missed_calls"] = sum(call["is_missed"] for call in calls)
        result["total_calls"] = len(calls)
        result["analyzable_calls"] = sum(bool(call["recording_url"]) for call in calls)
        result["completed_calls"] = sum(
            call["analysis_status"] == "completed" for call in calls
        )
        result["failed_calls"] = sum(
            call["analysis_status"] == "error" for call in calls
        )
        return result

    def calls_for_report(
        self, day: date, timezone: str, employee_id: str = "",
    ) -> list[dict]:
        with self._connect() as db:
            row = db.execute(
                "SELECT period_start,period_end FROM daily_reports WHERE report_day=?",
                (day.isoformat(),),
            ).fetchone()
        if row is None:
            return []
        start = str(row["period_start"] or "")
        end = str(row["period_end"] or "")
        if not start or not end:
            start, end = self._bounds(day, timezone)
        return self.calls_for_period(start, end, timezone, employee_id)
