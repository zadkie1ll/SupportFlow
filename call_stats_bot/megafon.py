from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(frozen=True)
class CallHistoryEvent:
    call_id: str
    employee_id: str
    started_at: datetime
    duration_seconds: int
    status: str


async def fetch_employee_names(
    *,
    api_url: str,
    api_key: str,
    auth_header: str,
    employee_ids: set[str],
) -> dict[str, str]:
    """Read current employee names from MegaFon CRM API `/users`."""
    if not api_url or not api_key:
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{api_url.rstrip('/')}/users",
            headers={auth_header: api_key},
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return {
        str(item.get("ext")): str(item.get("name")).strip()
        for item in items
        if isinstance(item, dict)
        and str(item.get("ext")) in employee_ids
        and str(item.get("name") or "").strip()
    }


def parse_history_event(data: dict[str, Any]) -> CallHistoryEvent | None:
    """Parse MegaFon/MCN-compatible CRM REST API `history` callback."""
    if str(data.get("cmd", "")).lower() != "history":
        return None

    call_id = str(data.get("callid", "")).strip()
    # В реальных callback МегаФон `user` — строковый логин сотрудника,
    # а выбранный пользователем внутренний номер находится в `ext`.
    employee_id = str(data.get("ext") or data.get("user", "")).strip()
    if not call_id or not employee_id:
        raise ValueError("history event requires callid and ext/user")

    try:
        duration = max(0, int(float(data.get("duration", 0))))
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be a number") from exc

    raw_start = str(data.get("start", "")).strip()
    if raw_start:
        try:
            started_at = datetime.strptime(raw_start, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise ValueError("start must have YYYYmmddTHHMMSSZ format") from exc
    else:
        started_at = datetime.now(timezone.utc)

    return CallHistoryEvent(
        call_id=call_id,
        employee_id=employee_id,
        started_at=started_at,
        duration_seconds=duration,
        status=str(data.get("status", "")),
    )
