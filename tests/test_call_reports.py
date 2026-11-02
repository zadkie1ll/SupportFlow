from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from call_stats_bot.analysis import AnalysisResult, DailyAnalysisService
from call_stats_bot.megafon import parse_history_event
from call_stats_bot.main import _active_report_day
from call_stats_bot.miniapp import verify_telegram_init_data
from call_stats_bot.store import CallStore


class FakeAnalyzer:
    transcription_model = "transcribe-test"
    summary_model = "summary-test"

    def analyze(self, call: dict) -> AnalysisResult:
        return AnalysisResult(
            transcript_text="Менеджер: Добрый день\n\nКлиент: Нужна помощь",
            segments=[
                {"speaker": "manager", "text": "Добрый день"},
                {"speaker": "client", "text": "Нужна помощь"},
            ],
            summary={
                "short_summary": "Клиент запросил помощь.",
                "client_request": "Помощь",
                "result": "Запрос принят",
                "next_steps": ["Перезвонить"],
                "important_facts": [],
            },
            transcription_model=self.transcription_model,
            summary_model=self.summary_model,
        )


class CallReportTests(unittest.TestCase):
    def test_old_calls_table_is_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calls.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute(
                    """CREATE TABLE calls(
                        call_id TEXT PRIMARY KEY, employee_id TEXT NOT NULL,
                        started_at TEXT NOT NULL, duration_seconds INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT '')"""
                )
                db.execute(
                    "INSERT INTO calls VALUES ('legacy','713','2026-10-30T10:00:00+00:00',60,'Success')"
                )
            store = CallStore(str(path))
            calls = store.calls_for_day(date(2026, 10, 30), "Europe/Moscow")
            self.assertEqual(calls[0]["call_id"], "legacy")
            self.assertEqual(calls[0]["recording_url"], "")

    def test_daily_analysis_is_persisted_and_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CallStore(str(Path(temporary) / "calls.sqlite3"))
            store.save_call(
                call_id="call-1", employee_id="713",
                started_at=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
                duration_seconds=80, status="Success", direction="in",
                client_phone="79990000001", recording_url="https://records.test/1.mp3",
            )
            service = DailyAnalysisService(
                store, FakeAnalyzer(), timezone="Europe/Moscow", concurrency=2
            )
            result = asyncio.run(service.run(date(2026, 10, 30), {"713": "Александр"}))
            self.assertEqual(result["completed"], 1)
            report = store.report_detail(date(2026, 10, 30), "Europe/Moscow")
            self.assertEqual(report["employees"]["713"]["analysis_completed"], 1)
            calls = store.calls_for_day(date(2026, 10, 30), "Europe/Moscow", "713")
            self.assertNotIn("segments", calls[0])
            self.assertEqual(calls[0]["summary_text"], "Клиент запросил помощь.")
            detail = store.call_detail("call-1", "Europe/Moscow")
            self.assertEqual(detail["segments"][1]["speaker"], "client")
            self.assertIn("Менеджер", detail["transcript_text"])

    def test_report_day_runs_from_previous_cutoff_to_current_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CallStore(str(Path(temporary) / "calls.sqlite3"))
            moments = {
                "before": datetime(2026, 11, 1, 16, 59, 59, tzinfo=UTC),
                "start": datetime(2026, 11, 1, 17, 0, 0, tzinfo=UTC),
                "inside": datetime(2026, 11, 2, 16, 59, 59, tzinfo=UTC),
                "end": datetime(2026, 11, 2, 17, 0, 0, tzinfo=UTC),
            }
            for call_id, started_at in moments.items():
                store.save_call(
                    call_id=call_id, employee_id="713", started_at=started_at,
                    duration_seconds=10, status="Success",
                    recording_url=f"https://records.test/{call_id}.mp3",
                )
            calls = store.calls_for_day(
                date(2026, 11, 2), "Europe/Moscow", report_time="20:00"
            )
            self.assertEqual({call["call_id"] for call in calls}, {"start", "inside"})
            stats = store.daily_stats(
                date(2026, 11, 2), "Europe/Moscow", ["713"], "20:00"
            )
            self.assertEqual(stats["713"].calls, 2)

    def test_next_report_catches_recording_delivered_after_older_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CallStore(str(Path(temporary) / "calls.sqlite3"))
            store.save_call(
                call_id="late-call", employee_id="713",
                started_at=datetime(2026, 10, 30, 17, 5, tzinfo=UTC),
                duration_seconds=27, status="Success",
                recording_url="https://records.test/late.mp3",
            )
            service = DailyAnalysisService(
                store, FakeAnalyzer(), timezone="Europe/Moscow", concurrency=2,
                report_time="20:00",
            )
            asyncio.run(service.run(date(2026, 11, 2), {"713": "Александр"}))
            detail = store.call_detail("late-call", "Europe/Moscow")
            self.assertEqual(detail["analysis_status"], "completed")
            report = store.report_detail(date(2026, 11, 2), "Europe/Moscow")
            self.assertEqual(report["period_start"], "2026-11-01T17:00:00+00:00")
            self.assertEqual(report["period_end"], "2026-11-02T17:00:00+00:00")

    def test_live_catch_up_processes_new_recording_without_daily_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CallStore(str(Path(temporary) / "calls.sqlite3"))
            store.save_call(
                call_id="live-call", employee_id="713",
                started_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
                duration_seconds=300, status="Success",
                recording_url="https://records.test/live.mp3",
            )
            service = DailyAnalysisService(
                store, FakeAnalyzer(), timezone="Europe/Moscow",
                report_time="20:00",
            )
            result = asyncio.run(service.catch_up(date(2026, 11, 2)))
            self.assertEqual(result["selected"], 1)
            self.assertEqual(
                store.call_detail("live-call", "Europe/Moscow")["analysis_status"],
                "completed",
            )
            self.assertIsNone(store.report_detail(date(2026, 11, 2), "Europe/Moscow"))

    def test_active_report_day_switches_exactly_at_cutoff(self):
        zone = ZoneInfo("Europe/Moscow")
        self.assertEqual(
            _active_report_day(datetime(2026, 11, 2, 19, 59, tzinfo=zone), "20:00"),
            date(2026, 11, 2),
        )
        self.assertEqual(
            _active_report_day(datetime(2026, 11, 2, 20, 0, tzinfo=zone), "20:00"),
            date(2026, 11, 3),
        )

    def test_missed_calls_are_counted_marked_and_sorted_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CallStore(str(Path(temporary) / "calls.sqlite3"))
            for call_id, minute, status, recording in (
                ("missed-newest", 30, "Busy", ""),
                ("success", 20, "Success", "https://records.test/success.mp3"),
                ("missed-oldest", 10, "NotAvailable", ""),
                ("legacy-success", 5, "Success", ""),
            ):
                store.save_call(
                    call_id=call_id, employee_id="713",
                    started_at=datetime(2026, 11, 2, 12, minute, tzinfo=UTC),
                    duration_seconds=10, status=status, recording_url=recording,
                )
            calls = store.calls_for_day(date(2026, 11, 2), "Europe/Moscow")
            self.assertEqual(
                [call["call_id"] for call in calls],
                ["success", "legacy-success", "missed-newest", "missed-oldest"],
            )
            self.assertEqual([call["is_missed"] for call in calls], [False, False, True, True])
            stats = store.daily_stats(
                date(2026, 11, 2), "Europe/Moscow", ["713"]
            )
            self.assertEqual(stats["713"].missed_calls, 2)

    def test_history_event_keeps_recording_and_call_metadata(self):
        event = parse_history_event({
            "cmd": "history", "callid": "abc", "ext": "713", "user": "manager",
            "start": "20261030T100000Z", "duration": "42", "status": "Success",
            "type": "out", "phone": "79990000001", "telnum": "78000000000",
            "link": "https://records.test/abc.mp3",
        })
        self.assertIsNotNone(event)
        self.assertEqual(event.recording_url, "https://records.test/abc.mp3")
        self.assertEqual((event.direction, event.client_phone, event.ats_user),
                         ("out", "79990000001", "manager"))

    def test_telegram_init_data_signature_and_admin_check(self):
        token = "123456:TEST_TOKEN"
        now = int(time.time())
        values = {
            "auth_date": str(now),
            "query_id": "query",
            "user": json.dumps({"id": 42, "first_name": "Admin"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        user = verify_telegram_init_data(
            urlencode(values), bot_token=token, admin_ids={42},
            max_age_seconds=3600, now=now,
        )
        self.assertEqual(user["id"], 42)
        with self.assertRaises(PermissionError):
            verify_telegram_init_data(
                urlencode(values), bot_token=token, admin_ids={7},
                max_age_seconds=3600, now=now,
            )


if __name__ == "__main__":
    unittest.main()
