from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request

from call_stats_bot.analysis import CallAnalyzer, DailyAnalysisService
from call_stats_bot.config import get_settings
from call_stats_bot.employee_roster import EmployeeRoster
from call_stats_bot.bitrix import BitrixClient
from call_stats_bot.megafon import fetch_employee_names, parse_history_event
from call_stats_bot.miniapp import router as miniapp_router
from call_stats_bot.report import build_daily_report
from call_stats_bot.store import CallStore
from call_stats_bot.substitutions import SubstitutionService, SubstitutionStore
from call_stats_bot.telegram import (
    answer_callback_query, edit_message, get_updates, send_message, web_app_keyboard,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("call-stats-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
_employee_roster: EmployeeRoster | None = None
_daily_analysis_service: DailyAnalysisService | None = None


def _store() -> CallStore:
    return CallStore(get_settings().database_path)


def _substitution_service() -> SubstitutionService | None:
    settings = get_settings()
    if not settings.bitrix_webhook_url and not settings.bitrix_gateway_url:
        return None
    client = BitrixClient(
        webhook_url=settings.bitrix_webhook_url,
        gateway_url=settings.bitrix_gateway_url,
        gateway_token=settings.bitrix_gateway_token,
        gateway_profile=settings.bitrix_gateway_profile,
    )
    return SubstitutionService(SubstitutionStore(settings.database_path), client)


async def _send_report(day: date) -> None:
    settings = get_settings()
    employees = await _employee_names()
    store = _store()
    store.begin_daily_report(day, settings.timezone, employees)
    text = build_daily_report(store, day, settings.timezone, employees)
    markup = _miniapp_markup()

    async def send_to_chat(chat_id: int) -> None:
        try:
            await send_message(settings.telegram_bot_token, chat_id, text, markup)
        except Exception:
            logger.exception("Cannot send daily report to chat_id=%s", chat_id)

    await asyncio.gather(*(send_to_chat(chat_id) for chat_id in settings.chat_ids))


def _miniapp_markup() -> dict | None:
    url = get_settings().normalized_miniapp_url
    return web_app_keyboard(url) if url else None


def _analysis_service() -> DailyAnalysisService | None:
    global _daily_analysis_service
    settings = get_settings()
    if not settings.analysis_enabled or not settings.openai_api_key:
        return None
    if _daily_analysis_service is None:
        analyzer = CallAnalyzer(
            api_key=settings.openai_api_key,
            transcription_model=settings.transcription_model,
            summary_model=settings.call_summary_model,
            download_timeout_seconds=settings.analysis_download_timeout_seconds,
            max_audio_bytes=settings.analysis_max_audio_bytes,
        )
        _daily_analysis_service = DailyAnalysisService(
            _store(), analyzer, timezone=settings.timezone,
            concurrency=settings.analysis_concurrency,
        )
    return _daily_analysis_service


async def _run_daily_analysis(day: date, *, notify: bool = True) -> dict[str, int | str]:
    settings = get_settings()
    employees = await _employee_names()
    service = _analysis_service()
    if service is None:
        store = _store()
        store.begin_daily_report(day, settings.timezone, employees)
        reason = (
            "Daily analysis is temporarily disabled"
            if not settings.analysis_enabled
            else "CALLBOT_OPENAI_API_KEY is not configured"
        )
        store.mark_daily_report_attention(day, reason)
        return {"status": "attention", "completed": 0, "failed": 0, "skipped": 0}
    result = await service.run(day, employees)
    if notify:
        text = (
            f"✅ <b>Анализ звонков за {day.strftime('%d.%m.%Y')} готов</b>\n"
            f"Расшифровано: {result['completed']} · Ошибок: {result['failed']} · "
            f"Без записи: {result['skipped']}"
        )
        await asyncio.gather(*(
            send_message(
                settings.telegram_bot_token, chat_id, text, _miniapp_markup()
            ) for chat_id in settings.chat_ids
        ), return_exceptions=True)
    return result


async def _employee_names() -> dict[str, str]:
    settings = get_settings()
    employees = dict(settings.employee_map)
    try:
        employees.update(
            await fetch_employee_names(
                api_url=settings.megafon_api_url,
                api_key=settings.megafon_api_key,
                auth_header=settings.megafon_api_auth_header,
                employee_ids=set(employees),
            )
        )
    except Exception:
        logger.exception("Cannot refresh employee names from MegaFon; using configured names")
    return employees


async def _report_loop() -> None:
    settings = get_settings()
    hour, minute = (int(part) for part in settings.report_time.split(":"))
    zone = ZoneInfo(settings.timezone)
    while True:
        now = datetime.now(zone)
        next_run = datetime.combine(now.date(), time(hour, minute), tzinfo=zone)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        await _send_report(next_run.date())
        try:
            await _run_daily_analysis(next_run.date())
        except Exception:
            logger.exception("Daily call analysis failed for %s", next_run.date())
            _store().mark_daily_report_attention(
                next_run.date(), "Unexpected daily analysis failure"
            )


def _user_name(user: dict) -> str:
    name = " ".join(str(user.get(key, "")).strip() for key in ("NAME", "LAST_NAME")).strip()
    return name or str(user.get("EMAIL") or user.get("ID") or "Сотрудник")


def _inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


async def _mapped_employee_rows(service: SubstitutionService) -> list[tuple[str, int, str]]:
    rows = await _roster(service).rows()
    return [
        (row.extension, row.user_id, row.name)
        for row in rows
    ]


def _roster(service: SubstitutionService) -> EmployeeRoster:
    global _employee_roster
    if _employee_roster is None:
        settings = get_settings()
        _employee_roster = EmployeeRoster(
            service.bitrix,
            settings.telephony_bitrix_map,
            settings.employee_poll_seconds,
            _employee_names,
        )
    return _employee_roster


async def _send_source_keyboard(chat_id: int, service: SubstitutionService) -> None:
    settings = get_settings()
    rows = [
        [(f"{name} · {extension}", f"sub:src:{extension}")]
        for extension, _, name in await _mapped_employee_rows(service)
    ]
    await send_message(
        settings.telegram_bot_token,
        chat_id,
        "<b>Шаг 1 из 3</b>\nС какого менеджера временно снять сделки?",
        _inline_keyboard(rows),
    )


async def _send_date_keyboard(chat_id: int) -> None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    rows: list[list[tuple[str, str]]] = []
    today_20 = datetime.combine(now.date(), time(20, 0), tzinfo=now.tzinfo)
    if today_20 > now:
        rows.append([("Сегодня, 20:00", "sub:date:today20")])
    rows.extend(
        [
            [("Завтра, 09:00", "sub:date:tomorrow09"), ("Завтра, 20:00", "sub:date:tomorrow20")],
            [("Через 7 дней, 20:00", "sub:date:week20")],
            [("✍️ Ввести дату и время", "sub:date:custom")],
        ]
    )
    await send_message(
        settings.telegram_bot_token,
        chat_id,
        "<b>Шаг 2 из 3</b>\nДо какого времени действует замещение?",
        _inline_keyboard(rows),
    )


async def _send_target_keyboard(
    chat_id: int, service: SubstitutionService, source_extension: str, ends_at: datetime
) -> None:
    settings = get_settings()
    zone = ZoneInfo(settings.timezone)
    rows = [
        [(f"{name} · {extension}", f"sub:dst:{extension}")]
        for row in await _roster(service).rows(active_only=True)
        for extension, name in [(row.extension, row.name)]
        if extension != source_extension
    ]
    await send_message(
        settings.telegram_bot_token,
        chat_id,
        (
            "<b>Шаг 3 из 3</b>\n"
            f"Возврат: <b>{ends_at.astimezone(zone).strftime('%d.%m.%Y %H:%M')}</b>\n"
            "На кого временно перевести сделки?"
        ),
        _inline_keyboard(rows),
    )


async def _create_preview(
    *, chat_id: int, sender_id: int, source_extension: str, target_extension: str,
    ends_at: datetime, service: SubstitutionService
) -> None:
    settings = get_settings()
    mapping = settings.telephony_bitrix_map
    if source_extension not in mapping or target_extension not in mapping:
        raise ValueError("Выбран сотрудник вне разрешённого списка телефонии")
    visible_extensions = {row.extension for row in await _roster(service).rows()}
    active_extensions = {
        row.extension for row in await _roster(service).rows(active_only=True)
    }
    if source_extension not in visible_extensions:
        raise ValueError("У исходного сотрудника нет активных сделок")
    if target_extension not in active_extensions:
        raise ValueError("Замещающий сотрудник уволен или неактивен")
    if source_extension == target_extension:
        raise ValueError("Источник и замещающий сотрудник должны отличаться")
    source_id, target_id = mapping[source_extension], mapping[target_extension]
    source, target = await asyncio.gather(
        service.bitrix.get_user(source_id), service.bitrix.get_user(target_id)
    )
    operation = service.store.create_draft(
        source_user_id=source_id,
        target_user_id=target_id,
        source_name=_user_name(source),
        target_name=_user_name(target),
        source_extension=source_extension,
        target_extension=target_extension,
        ends_at=ends_at,
        created_by=sender_id,
    )
    try:
        count = await service.preview(operation)
    except Exception:
        service.store.cancel_draft(operation.id)
        raise
    service.store.clear_wizard(sender_id)
    zone = ZoneInfo(settings.timezone)
    await send_message(
        settings.telegram_bot_token,
        chat_id,
        (
            f"<b>Проверьте замещение</b>\n"
            f"{escape(operation.source_name)} ({source_extension}) → "
            f"{escape(operation.target_name)} ({target_extension})\n"
            f"Активных сделок: <b>{count}</b>\n"
            f"Автовозврат: <b>{ends_at.astimezone(zone).strftime('%d.%m.%Y %H:%M')}</b>\n\n"
            "Изменения начнутся только после подтверждения."
        ),
        _inline_keyboard(
            [
                [("✅ Подтвердить", f"sub:confirm:{operation.id}")],
                [("❌ Отменить", f"sub:cancel:{operation.id}")],
            ]
        ),
    )


async def _send_replacements(chat_id: int, service: SubstitutionService) -> None:
    settings = get_settings()
    operations = service.store.recent()
    lines = ["<b>Последние замещения</b>"]
    buttons: list[list[tuple[str, str]]] = []
    if not operations:
        lines.append("Операций пока нет.")
    for operation in operations:
        counts = service.store.counts(operation.id)
        total = sum(counts.values())
        details = (
            f"перенесено {counts.get('applied', 0)}/{total}, "
            f"ожидает {counts.get('planned', 0)}, ошибок {counts.get('failed', 0)}, "
            f"конфликтов {counts.get('conflict', 0)}"
        )
        if operation.status in {"restoring", "completed", "attention"}:
            details += f", восстановлено {counts.get('restored', 0)}"
        lines.append(
            f"<code>{operation.id[:8]}</code> {escape(operation.source_name)} → "
            f"{escape(operation.target_name)} · {operation.source_extension}→{operation.target_extension} "
            f"· {operation.status}\n{details}"
        )
        if operation.status in {"active", "attention"}:
            buttons.append(
                [(f"Вернуть {operation.source_extension} сейчас", f"sub:restore:{operation.id}")]
            )
    buttons.append([("➕ Создать замещение", "sub:new")])
    await send_message(
        settings.telegram_bot_token, chat_id, "\n".join(lines), _inline_keyboard(buttons)
    )


def _preset_deadline(value: str, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    if value == "today20":
        result = datetime.combine(now.date(), time(20, 0), tzinfo=zone)
    elif value == "tomorrow09":
        result = datetime.combine(now.date() + timedelta(days=1), time(9, 0), tzinfo=zone)
    elif value == "tomorrow20":
        result = datetime.combine(now.date() + timedelta(days=1), time(20, 0), tzinfo=zone)
    elif value == "week20":
        result = datetime.combine(now.date() + timedelta(days=7), time(20, 0), tzinfo=zone)
    else:
        raise ValueError("Неизвестный вариант даты")
    if result <= now:
        raise ValueError("Выбранное время уже прошло")
    return result


def _progress_text(operation, counts: dict[str, int], timezone_name: str) -> str:
    total = sum(counts.values())
    restoring = operation.status in {"restoring", "completed", "attention"}
    if restoring:
        completed = counts.get("restored", 0) + counts.get("conflict", 0)
        action = "Возврат сделок"
        primary = f"Восстановлено: <b>{counts.get('restored', 0)} / {total}</b>"
        pending = counts.get("applied", 0) + counts.get("planned", 0) + counts.get("failed", 0)
    else:
        completed = counts.get("applied", 0) + counts.get("conflict", 0)
        action = "Перенос сделок"
        primary = f"Перенесено: <b>{counts.get('applied', 0)} / {total}</b>"
        pending = counts.get("planned", 0) + counts.get("failed", 0)
    percent = 100 if total == 0 else min(100, int(completed * 100 / total))
    filled = min(10, percent // 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    zone = ZoneInfo(timezone_name)
    return (
        f"<b>{action}</b>\n"
        f"{escape(operation.source_name)} ({operation.source_extension}) → "
        f"{escape(operation.target_name)} ({operation.target_extension})\n\n"
        f"{bar} <b>{percent}%</b>\n"
        f"{primary}\n"
        f"Ожидает: {pending} · Ошибки: {counts.get('failed', 0)} · "
        f"Конфликты: {counts.get('conflict', 0)}\n"
        f"Автовозврат: {operation.ends_at.astimezone(zone).strftime('%d.%m.%Y %H:%M')}"
    )


def _progress_markup(operation) -> dict:
    rows: list[list[tuple[str, str]]] = [[("📋 Все операции", "sub:list")]]
    if operation.status == "active":
        rows.insert(0, [("↩️ Вернуть сейчас", f"sub:restore:{operation.id}")])
    return _inline_keyboard(rows)


async def _show_progress(
    operation, service: SubstitutionService, chat_id: int, message_id: int | None = None
) -> None:
    settings = get_settings()
    current = service.store.get(operation.id)
    counts = service.store.counts(current.id)
    text = _progress_text(current, counts, settings.timezone)
    markup = _progress_markup(current)
    target_message_id = message_id or current.progress_message_id
    target_chat_id = chat_id if message_id else (current.progress_chat_id or chat_id)
    if target_message_id:
        await edit_message(
            settings.telegram_bot_token, target_chat_id, target_message_id, text, markup
        )
    else:
        target_message_id = await send_message(
            settings.telegram_bot_token, target_chat_id, text, markup
        )
    service.store.set_progress_message(current.id, target_chat_id, target_message_id)


async def _handle_callback(update: dict, service: SubstitutionService | None) -> None:
    settings = get_settings()
    callback = update["callback_query"]
    callback_id = str(callback.get("id", ""))
    sender_id = (callback.get("from") or {}).get("id")
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    data = str(callback.get("data", ""))
    with suppress(Exception):
        await asyncio.wait_for(
            answer_callback_query(settings.telegram_bot_token, callback_id), timeout=4
        )
    if sender_id not in settings.admin_ids or not chat_id:
        return
    if service is None:
        await send_message(settings.telegram_bot_token, int(chat_id), "Bitrix24 ещё не настроен.")
        return
    try:
        if data == "sub:new":
            service.store.start_wizard(int(sender_id), int(chat_id))
            await _send_source_keyboard(int(chat_id), service)
        elif data.startswith("sub:src:"):
            extension = data.rsplit(":", 1)[1]
            if extension not in settings.telephony_bitrix_map:
                raise ValueError("Этот сотрудник не входит в список телефонии")
            try:
                service.store.get_wizard(int(sender_id))
            except ValueError:
                service.store.start_wizard(int(sender_id), int(chat_id))
            service.store.set_wizard_source(int(sender_id), extension)
            await _send_date_keyboard(int(chat_id))
        elif data == "sub:date:custom":
            service.store.await_wizard_date(int(sender_id))
            await send_message(
                settings.telegram_bot_token,
                int(chat_id),
                "Напишите дату и время возврата сообщением, например: <code>15.10.2026 20:00</code>",
            )
        elif data.startswith("sub:date:"):
            ends_at = _preset_deadline(data.rsplit(":", 1)[1], settings.timezone)
            wizard = service.store.set_wizard_date(int(sender_id), ends_at)
            await _send_target_keyboard(int(chat_id), service, wizard.source_extension, ends_at)
        elif data.startswith("sub:dst:"):
            target_extension = data.rsplit(":", 1)[1]
            wizard = service.store.get_wizard(int(sender_id))
            if wizard.state != "choose_target" or wizard.ends_at is None:
                raise ValueError("Сначала выберите дату возврата")
            await _create_preview(
                chat_id=int(chat_id), sender_id=int(sender_id),
                source_extension=wizard.source_extension,
                target_extension=target_extension, ends_at=wizard.ends_at, service=service,
            )
        elif data.startswith("sub:confirm:"):
            operation = service.store.get(data.split(":", 2)[2])
            await service.activate(operation)
            callback_message_id = int((callback.get("message") or {}).get("message_id", 0)) or None
            await _show_progress(
                operation, service, int(chat_id), callback_message_id
            )
        elif data.startswith("sub:cancel:"):
            operation = service.store.get(data.split(":", 2)[2])
            service.store.cancel_draft(operation.id)
            await send_message(
                settings.telegram_bot_token, int(chat_id),
                "Черновик отменён. Сделки в Bitrix24 не изменялись.",
                _inline_keyboard([[("➕ Создать заново", "sub:new")]]),
            )
        elif data.startswith("sub:restore:"):
            operation = service.store.get(data.split(":", 2)[2])
            await service.start_restore(operation)
            await _show_progress(operation, service, int(chat_id))
        elif data == "sub:list":
            await _send_replacements(int(chat_id), service)
    except Exception as exc:
        logger.exception("Telegram substitution callback failed")
        if "Сначала выберите сотрудника" in str(exc):
            service.store.start_wizard(int(sender_id), int(chat_id))
            await send_message(
                settings.telegram_bot_token,
                int(chat_id),
                "Эта кнопка относится к предыдущему выбору. Начнём заново.",
            )
            await _send_source_keyboard(int(chat_id), service)
            return
        await send_message(
            settings.telegram_bot_token, int(chat_id),
            f"Операция не выполнена: {escape(str(exc))}",
            _inline_keyboard([[("Начать заново", "sub:new")]]),
        )


async def _handle_telegram_update(update: dict, service: SubstitutionService | None) -> None:
    settings = get_settings()
    if update.get("callback_query"):
        await _handle_callback(update, service)
        return
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    sender_id = (message.get("from") or {}).get("id")
    text = str(message.get("text", "")).strip()
    if not chat_id or not text:
        return
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    if command in {"/today", "/start"}:
        if sender_id not in settings.admin_ids:
            return
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        report = build_daily_report(_store(), today, settings.timezone, await _employee_names())
        if command == "/start":
            report += (
                "\n\n<b>Замещения в Bitrix24</b>\n"
                "/replace ВНУТРЕННИЙ_СТАРОГО ВНУТРЕННИЙ_НОВОГО ГГГГ-ММ-ДД ЧЧ:ММ\n"
                "/confirm КОД — подтвердить\n"
                "/cancel КОД — отменить черновик\n"
                "/restore КОД — вернуть сейчас\n"
                "/bitrix_users — сотрудники и ID\n"
                "/replacements — состояние"
            )
        markup = _miniapp_markup()
        if command == "/start" and service is not None:
            rows = list((markup or {}).get("inline_keyboard", []))
            rows.extend(
                _inline_keyboard(
                    [[("➕ Создать замещение", "sub:new")],
                     [("📋 Текущие операции", "sub:list")]]
                )["inline_keyboard"]
            )
            markup = {"inline_keyboard": rows}
        await send_message(settings.telegram_bot_token, int(chat_id), report, markup)
        return

    if sender_id not in settings.admin_ids:
        return
    if command == "/analyze_today":
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        await send_message(
            settings.telegram_bot_token, int(chat_id),
            f"Запускаю анализ звонков за {today.strftime('%d.%m.%Y')}. Результат сохранится в отчёте.",
            _miniapp_markup(),
        )
        asyncio.create_task(_run_daily_analysis(today))
        return
    if service is None:
        await send_message(settings.telegram_bot_token, int(chat_id), "Bitrix24 ещё не настроен.")
        return

    try:
        if not text.startswith("/"):
            wizard = service.store.get_wizard(int(sender_id))
            if wizard.state != "await_date":
                return
            parsed = None
            for date_format in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, date_format)
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise ValueError("Дата должна быть в формате ДД.ММ.ГГГГ ЧЧ:ММ")
            ends_at = parsed.replace(tzinfo=ZoneInfo(settings.timezone))
            if ends_at <= datetime.now(ZoneInfo(settings.timezone)):
                raise ValueError("Дата возврата должна быть в будущем")
            wizard = service.store.set_wizard_date(int(sender_id), ends_at)
            await _send_target_keyboard(int(chat_id), service, wizard.source_extension, ends_at)
            return
        if command == "/replace":
            parts = text.split()
            if len(parts) == 1:
                service.store.start_wizard(int(sender_id), int(chat_id))
                await _send_source_keyboard(int(chat_id), service)
                return
            if len(parts) != 5:
                raise ValueError("Формат: /replace ВНУТРЕННИЙ_СТАРОГО ВНУТРЕННИЙ_НОВОГО ГГГГ-ММ-ДД ЧЧ:ММ")
            source_extension, target_extension = parts[1], parts[2]
            mapping = settings.telephony_bitrix_map
            if source_extension not in mapping or target_extension not in mapping:
                allowed = ", ".join(sorted(mapping))
                raise ValueError(f"Разрешены только сотрудники телефонии: {allowed}")
            local_end = datetime.strptime(f"{parts[3]} {parts[4]}", "%Y-%m-%d %H:%M")
            ends_at = local_end.replace(tzinfo=ZoneInfo(settings.timezone))
            await _create_preview(
                chat_id=int(chat_id), sender_id=int(sender_id),
                source_extension=source_extension, target_extension=target_extension,
                ends_at=ends_at, service=service,
            )
        elif command == "/confirm":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Формат: /confirm КОД")
            operation = service.store.get(parts[1])
            await service.activate(operation)
            await _show_progress(operation, service, int(chat_id))
        elif command == "/cancel":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Формат: /cancel КОД")
            operation = service.store.get(parts[1])
            service.store.cancel_draft(operation.id)
            await send_message(
                settings.telegram_bot_token, int(chat_id),
                f"Черновик <code>{operation.id[:8]}</code> отменён. Сделки в Bitrix24 не изменялись.",
            )
        elif command == "/restore":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Формат: /restore КОД")
            operation = service.store.get(parts[1])
            await service.start_restore(operation)
            await _show_progress(operation, service, int(chat_id))
        elif command == "/replacements":
            await _send_replacements(int(chat_id), service)
        elif command == "/bitrix_users":
            lines = ["<b>Сотрудники телефонии, доступные для замещения</b>"]
            for row in await _roster(service).rows():
                note = "" if row.active else " · уволен, есть активные сделки"
                lines.append(
                    f"<code>{row.extension}</code> — {escape(row.name)} "
                    f"<i>(Bitrix {row.user_id}{note})</i>"
                )
            await send_message(settings.telegram_bot_token, int(chat_id), "\n".join(lines))
    except Exception as exc:
        logger.exception("Telegram substitution command failed")
        await send_message(settings.telegram_bot_token, int(chat_id), f"Операция не выполнена: {escape(str(exc))}")


async def _telegram_poll_loop(service: SubstitutionService | None) -> None:
    settings = get_settings()
    offset: int | None = None
    timeout = httpx.Timeout(35, connect=5, write=10, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                updates = await get_updates(settings.telegram_bot_token, offset, client)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    await _handle_telegram_update(update, service)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Telegram polling temporarily unavailable", exc_info=True)
                await asyncio.sleep(2)


async def _substitution_loop(service: SubstitutionService) -> None:
    settings = get_settings()
    last_progress: dict[str, tuple] = {}
    while True:
        for operation in service.store.active():
            previous = operation.status
            before_counts = service.store.counts(operation.id)
            try:
                await service.reconcile(operation)
                current = service.store.get(operation.id)
                after_counts = service.store.counts(operation.id)
                signature = (current.status, *sorted(after_counts.items()))
                if (
                    current.progress_chat_id is not None
                    and current.progress_message_id is not None
                    and last_progress.get(current.id) != signature
                ):
                    try:
                        await _show_progress(
                            current, service, current.progress_chat_id, current.progress_message_id
                        )
                        last_progress[current.id] = signature
                    except Exception:
                        logger.exception("Cannot update progress for %s", current.id)
                before_pending = before_counts.get("planned", 0) + before_counts.get("failed", 0)
                after_pending = after_counts.get("planned", 0) + after_counts.get("failed", 0)
                if (
                    current.status == "active"
                    and before_pending > 0
                    and after_pending == 0
                    and current.created_by in settings.admin_ids
                ):
                    await send_message(
                        settings.telegram_bot_token,
                        current.created_by,
                        (
                            f"✅ Перенос <code>{current.id[:8]}</code> завершён: "
                            f"{after_counts.get('applied', 0)} сделок. "
                            f"Конфликтов: {after_counts.get('conflict', 0)}."
                        ),
                        _inline_keyboard([[("Посмотреть операции", "sub:list")]]),
                    )
                if current.status != previous and current.status in {"completed", "attention"}:
                    text = (
                        f"Замещение <code>{current.id[:8]}</code>: {current.status}. "
                        f"Восстановлено {after_counts.get('restored', 0)}, "
                        f"конфликтов {after_counts.get('conflict', 0)}, ошибок {after_counts.get('failed', 0)}."
                    )
                    await asyncio.gather(*(
                        send_message(settings.telegram_bot_token, chat_id, text)
                        for chat_id in settings.chat_ids
                    ), return_exceptions=True)
            except Exception:
                logger.exception("Substitution reconciliation failed for %s", operation.id)
        await asyncio.sleep(min(settings.substitution_poll_seconds, 5))


@asynccontextmanager
async def lifespan(_: FastAPI):
    _store()
    service = _substitution_service()
    tasks = [asyncio.create_task(_report_loop())]
    if service is not None:
        tasks.append(asyncio.create_task(_substitution_loop(service)))
        tasks.append(asyncio.create_task(_roster(service).polling_loop()))
    if get_settings().telegram_polling_enabled:
        tasks.append(asyncio.create_task(_telegram_poll_loop(service)))
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="MegaFon call stats bot", lifespan=lifespan)
app.include_router(miniapp_router)


async def _request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    return dict(await request.form())


@app.post("/megafon/webhook")
async def megafon_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()
    data = await _request_data(request)
    supplied_token = str(
        request.headers.get("X-CRM-AUTH")
        or request.headers.get("X-Megafon-Auth")
        or request.query_params.get("auth")
        or data.get("crm_token")
        or data.get("auth")
        or ""
    )
    if not hmac.compare_digest(supplied_token, settings.megafon_crm_token):
        raise HTTPException(status_code=403, detail="Invalid CRM token")

    try:
        event = parse_history_event(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event is None:
        return {"status": "ignored"}
    if event.employee_id not in settings.employee_map:
        return {"status": "employee_ignored"}

    _store().save_call(
        call_id=event.call_id,
        employee_id=event.employee_id,
        started_at=event.started_at,
        duration_seconds=event.duration_seconds,
        status=event.status,
        direction=event.direction,
        client_phone=event.client_phone,
        company_phone=event.company_phone,
        recording_url=event.recording_url,
        ats_user=event.ats_user,
    )
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid Telegram secret")
    update = await request.json()
    await _handle_telegram_update(update, _substitution_service())
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
