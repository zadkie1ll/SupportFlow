from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

from call_stats_bot.config import get_settings
from call_stats_bot.store import CallStore


router = APIRouter()


def verify_telegram_init_data(
    init_data: str, *, bot_token: str, admin_ids: set[int],
    max_age_seconds: int, now: int | None = None,
) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("Telegram signature is missing")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise ValueError("Invalid Telegram signature")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Telegram authorization data") from exc
    current = int(now if now is not None else time.time())
    if auth_date > current + 60 or current - auth_date > max_age_seconds:
        raise ValueError("Telegram authorization has expired")
    if user_id not in admin_ids:
        raise PermissionError("Administrator access required")
    return user


def _init_data(authorization: str = Header(default="")) -> str:
    prefix, separator, value = authorization.partition(" ")
    if not separator or prefix.casefold() != "tma" or not value:
        raise HTTPException(status_code=401, detail="Open the report from Telegram")
    return value


def require_admin(init_data: str = Depends(_init_data)) -> dict:
    settings = get_settings()
    try:
        return verify_telegram_init_data(
            init_data,
            bot_token=settings.telegram_bot_token,
            admin_ids=settings.admin_ids,
            max_age_seconds=settings.miniapp_auth_max_age_seconds,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _store() -> CallStore:
    return CallStore(get_settings().database_path)


@router.get("/miniapp", include_in_schema=False)
async def miniapp_page() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "miniapp.html")


@router.get("/api/reports")
async def reports(
    limit: int = Query(default=31, ge=1, le=180), _: dict = Depends(require_admin)
) -> dict:
    return {"items": _store().recent_reports(limit)}


@router.get("/api/reports/{report_day}")
async def report_detail(report_day: date, _: dict = Depends(require_admin)) -> dict:
    settings = get_settings()
    result = _store().report_detail(report_day, settings.timezone)
    if result is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return result


@router.get("/api/reports/{report_day}/employees/{extension}/calls")
async def employee_calls(
    report_day: date, extension: str, _: dict = Depends(require_admin)
) -> dict:
    settings = get_settings()
    if extension not in settings.employee_map:
        raise HTTPException(status_code=404, detail="Employee not found")
    calls = _store().calls_for_day(report_day, settings.timezone, extension)
    return {"day": report_day.isoformat(), "extension": extension, "items": calls}


@router.get("/api/calls/{call_id}")
async def call_detail(call_id: str, _: dict = Depends(require_admin)) -> dict:
    result = _store().call_detail(call_id, get_settings().timezone)
    if result is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return result
