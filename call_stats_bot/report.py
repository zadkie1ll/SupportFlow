from __future__ import annotations

from datetime import date, timedelta
from html import escape

from call_stats_bot.store import CallStore


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин {secs:02d} сек"
    if minutes:
        return f"{minutes} мин {secs:02d} сек"
    return f"{secs} сек"


def format_call_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "звонок"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        word = "звонка"
    else:
        word = "звонков"
    return f"{count} {word}"


def build_daily_report(
    store: CallStore,
    day: date,
    timezone: str,
    employees: dict[str, str],
    report_time: str | None = None,
) -> str:
    stats = store.daily_stats(day, timezone, list(employees), report_time)
    period = (
        f"с {(day - timedelta(days=1)).strftime('%d.%m')} "
        f"{report_time} до {day.strftime('%d.%m')} {report_time}"
        if report_time else day.strftime('%d.%m.%Y')
    )
    lines = [f"☎️ <b>Звонки за период</b>", f"🕗 {period} МСК", ""]
    for employee_id, name in employees.items():
        item = stats[employee_id]
        lines.append(
            f"<b>{escape(name)}</b>: {format_call_count(item.calls)} · "
            f"{format_duration(item.duration_seconds)} · "
            f"недозвонов: {item.missed_calls}"
        )
    return "\n".join(lines)
