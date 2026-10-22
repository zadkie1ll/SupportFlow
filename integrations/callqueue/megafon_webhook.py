import json
import logging
from urllib.parse import parse_qsl

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import BitrixSyncLog

router = APIRouter(prefix="/queue/megafon", tags=["megafon-webhook"])
logger = logging.getLogger(__name__)


def _forward_copy(
    url: str,
    body: bytes,
    content_type: str,
    headers_to_copy: dict[str, str],
    query_string: str,
) -> None:
    """Best effort: ошибка получателя копии не влияет на основной webhook."""
    if not url:
        return
    try:
        forward_headers = dict(headers_to_copy)
        if content_type:
            forward_headers["Content-Type"] = content_type
        if query_string:
            url = f"{url}?{query_string}"
        requests.post(url, data=body, headers=forward_headers, timeout=10)
    except requests.RequestException:
        logger.warning("megafon_webhook: forwarding copy to %s failed", url, exc_info=True)


def _extract_call_id(payload: dict) -> str:
    return str(payload.get("callid") or payload.get("call_id") or payload.get("callId") or payload.get("id") or "")


def _normalize_payload_bytes(body: bytes, content_type: str) -> dict:
    """Соответствует normalize_megafon_payload (call_queue/views.py:100) — сперва form-данные,
    иначе пытаемся распарсить JSON-тело. Парсит из уже прочитанных байт (а не заново из
    request.stream()), т.к. тело читается один раз в самом начале обработчика — Starlette не
    даёт прочитать ASGI-поток дважды (form()/body() после form() падают "Stream consumed")."""
    if not body:
        return {}
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(body.decode("utf-8", errors="ignore")))
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw_body": body.decode("utf-8", errors="ignore")}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


@router.post("/webhook")
async def megafon_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Соответствует megafon_webhook (call_queue/views.py:1461).
    Публичный эндпоинт — вызывается самим МегаФоном, без нашего JWT."""
    expected_key = settings.megafon_vats_crm_auth_key
    received_key = (
        request.headers.get("X-CRM-AUTH")
        or request.headers.get("X-Megafon-Auth")
        or request.query_params.get("auth")
    )

    raw_body = await request.body()
    content_type = request.headers.get("content-type", "")
    payload = _normalize_payload_bytes(raw_body, content_type)

    forward_urls = [
        settings.megafon_webhook_forward_url,
        settings.megafon_stats_forward_url,
    ]
    if any(forward_urls):
        headers_to_copy = {
            k: v for k, v in {
                "X-CRM-AUTH": request.headers.get("X-CRM-AUTH"),
                "X-Megafon-Auth": request.headers.get("X-Megafon-Auth"),
            }.items() if v
        }
        for forward_url in forward_urls:
            if forward_url:
                background_tasks.add_task(
                    _forward_copy,
                    forward_url,
                    raw_body,
                    content_type,
                    headers_to_copy,
                    str(request.query_params),
                )

    if not received_key:
        received_key = payload.get("crm_token") or payload.get("auth")
    call_id = _extract_call_id(payload)

    if not expected_key or received_key != expected_key:
        db.add(
            BitrixSyncLog(
                entity_type="megafon_webhook",
                entity_id=call_id,
                action="incoming_callback_rejected",
                request_payload={"payload": payload},
                response_payload={"ok": False},
                success=False,
                error_text="Invalid Megafon webhook auth key",
            )
        )
        db.commit()
        return Response(content="Invalid auth key", status_code=403)

    db.add(
        BitrixSyncLog(
            entity_type="megafon_webhook",
            entity_id=call_id,
            action=f"{payload.get('cmd', 'callback')}:{payload.get('type', payload.get('status', 'received'))}",
            request_payload={"payload": payload},
            response_payload={"accepted": True},
            success=True,
        )
    )
    db.commit()
    return {"ok": True}
