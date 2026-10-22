from __future__ import annotations

from datetime import date
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
) -> str:
    stats = store.daily_stats(day, timezone, list(employees))
    lines = [f"☎️ <b>Звонки за {day.strftime('%d.%m.%Y')}</b>", ""]
    for employee_id, name in employees.items():
        item = stats[employee_id]
        lines.append(
            f"<b>{escape(name)}</b>: {format_call_count(item.calls)} · "
            f"{format_duration(item.duration_seconds)}"
        )
    return "\n".join(lines)
