from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from call_stats_bot.config import get_settings
from call_stats_bot.roles import load_roles
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
    return CallStore(get_settings().database_target)


def _public_call(item: dict) -> dict:
    """Expose recording availability without leaking the ATS recording URL."""
    result = dict(item)
    result["has_recording"] = bool(result.pop("recording_url", ""))
    return result


async def _close_recording(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


@router.get("/support", include_in_schema=False)
@router.get("/support/", include_in_schema=False)
async def miniapp_page() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "miniapp.html")


@router.get("/api/support/reports")
async def reports(
    limit: int = Query(default=31, ge=1, le=180), _: dict = Depends(require_admin)
) -> dict:
    return {"items": _store().recent_reports(limit)}


@router.get("/api/support/reports/{report_day}")
async def report_detail(report_day: date, _: dict = Depends(require_admin)) -> dict:
    settings = get_settings()
    result = _store().report_detail(
        report_day, settings.timezone, set(settings.report_employee_ids)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Report not found")
    roles = load_roles()
    for extension, employee in result["employees"].items():
        role = roles.for_employee(extension)
        employee["role_key"] = role.key
        employee["role_title"] = role.title
        employee["role_objective"] = role.objective
        employee["role_responsibilities"] = list(role.responsibilities)
    return result


@router.get("/api/support/reports/{report_day}/employees/{extension}/calls")
async def employee_calls(
    report_day: date, extension: str, _: dict = Depends(require_admin)
) -> dict:
    settings = get_settings()
    if extension not in settings.report_employee_ids:
        raise HTTPException(status_code=404, detail="Employee not found")
    calls = _store().calls_for_report(report_day, settings.timezone, extension)
    return {
        "day": report_day.isoformat(),
        "extension": extension,
        "items": [_public_call(call) for call in calls],
    }


@router.get("/api/support/calls/{call_id}")
async def call_detail(call_id: str, _: dict = Depends(require_admin)) -> dict:
    result = _store().call_detail(call_id, get_settings().timezone)
    if result is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return _public_call(result)


@router.get("/api/support/calls/{call_id}/recording")
async def call_recording(
    call_id: str,
    range_header: str = Header(default="", alias="Range"),
    _: dict = Depends(require_admin),
) -> StreamingResponse:
    settings = get_settings()
    call = _store().call_detail(call_id, settings.timezone)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    recording_url = str(call.get("recording_url") or "").strip()
    if not recording_url:
        raise HTTPException(status_code=404, detail="Recording is not available")

    source_url = settings.recording_relay_url.strip() or recording_url
    params = {"url": recording_url} if settings.recording_relay_url.strip() else None
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20))
    try:
        request = client.build_request(
            "GET", source_url, params=params,
            headers={"Range": range_header} if range_header else None,
        )
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"Recording server returned {status}"
            )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail="Recording server is unavailable"
        ) from exc

    headers = {
        "Cache-Control": "private, no-store",
        **{
            key: value for key, value in response.headers.items()
            if key.casefold() in {
                "content-type", "content-length", "content-range", "accept-ranges",
            }
        },
    }
    return StreamingResponse(
        response.aiter_bytes(),
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("content-type", "audio/mpeg").split(";", 1)[0],
        background=BackgroundTask(_close_recording, response, client),
    )
