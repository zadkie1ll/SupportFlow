from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from call_stats_bot.config import get_settings
from call_stats_bot.employee_roster import EmployeeRoster
from call_stats_bot.roles import load_roles
from call_stats_bot.store import CallStore
from call_stats_bot.substitutions import Substitution, SubstitutionService


router = APIRouter()


class SubstitutionPreviewRequest(BaseModel):
    source_extension: str
    target_extension: str
    ends_at: datetime


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


def _substitution_runtime(request: Request) -> tuple[SubstitutionService, EmployeeRoster]:
    service = getattr(request.app.state, "substitution_service", None)
    roster = getattr(request.app.state, "employee_roster", None)
    if service is None or roster is None:
        raise HTTPException(status_code=503, detail="Bitrix24 integration is not configured")
    return service, roster


def _public_substitution(service: SubstitutionService, operation: Substitution) -> dict[str, Any]:
    counts = service.store.counts(operation.id)
    total = sum(counts.values())
    return {
        "id": operation.id,
        "source_extension": operation.source_extension,
        "target_extension": operation.target_extension,
        "source_name": operation.source_name,
        "target_name": operation.target_name,
        "ends_at": operation.ends_at.isoformat(),
        "status": operation.status,
        "counts": counts,
        "total": total,
    }


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


@router.get("/api/support/substitutions")
async def substitutions(
    request: Request, _: dict = Depends(require_admin),
) -> dict:
    service, roster = _substitution_runtime(request)
    employees = [
        {
            "extension": row.extension,
            "name": row.name,
            "active": row.active,
        }
        for row in await roster.rows()
    ]
    operations = [
        _public_substitution(service, operation)
        for operation in service.store.recent(30)
        if operation.status != "cancelled"
    ]
    return {"employees": employees, "operations": operations}


@router.post("/api/support/substitutions/preview")
async def preview_substitution(
    payload: SubstitutionPreviewRequest,
    request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    service, roster = _substitution_runtime(request)
    settings = get_settings()
    mapping = settings.telephony_bitrix_map
    source_extension = payload.source_extension.strip()
    target_extension = payload.target_extension.strip()
    if source_extension not in mapping or target_extension not in mapping:
        raise HTTPException(status_code=400, detail="Выбран сотрудник вне списка телефонии")
    if source_extension == target_extension:
        raise HTTPException(status_code=400, detail="Выберите разных сотрудников")
    ends_at = payload.ends_at
    if ends_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="Дата возврата должна содержать часовой пояс")
    if ends_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Дата возврата должна быть в будущем")

    rows = {row.extension: row for row in await roster.rows()}
    source = rows.get(source_extension)
    target = rows.get(target_extension)
    if source is None:
        raise HTTPException(status_code=400, detail="У исходного сотрудника нет активных сделок")
    if target is None or not target.active:
        raise HTTPException(status_code=400, detail="Замещающий сотрудник уволен или неактивен")

    try:
        service.store.cancel_drafts_by_creator(int(admin["id"]))
        operation = service.store.create_draft(
            source_user_id=mapping[source_extension],
            target_user_id=mapping[target_extension],
            source_name=source.name,
            target_name=target.name,
            source_extension=source_extension,
            target_extension=target_extension,
            ends_at=ends_at,
            created_by=int(admin["id"]),
        )
        try:
            await service.preview(operation)
        except Exception:
            service.store.cancel_draft(operation.id)
            raise
        return _public_substitution(service, service.store.get(operation.id))
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Не удалось получить сделки из Bitrix24") from exc


@router.post("/api/support/substitutions/{operation_id}/confirm")
async def confirm_substitution(
    operation_id: str,
    request: Request,
    _: dict = Depends(require_admin),
) -> dict:
    service, _ = _substitution_runtime(request)
    try:
        operation = service.store.get(operation_id)
        await service.activate(operation)
        return _public_substitution(service, service.store.get(operation.id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/support/substitutions/{operation_id}/cancel")
async def cancel_substitution(
    operation_id: str,
    request: Request,
    _: dict = Depends(require_admin),
) -> dict:
    service, _ = _substitution_runtime(request)
    try:
        service.store.cancel_draft(operation_id)
        return {"status": "cancelled"}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/support/substitutions/{operation_id}/restore")
async def restore_substitution(
    operation_id: str,
    request: Request,
    _: dict = Depends(require_admin),
) -> dict:
    service, _ = _substitution_runtime(request)
    try:
        operation = service.store.get(operation_id)
        await service.start_restore(operation)
        return _public_substitution(service, service.store.get(operation.id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
