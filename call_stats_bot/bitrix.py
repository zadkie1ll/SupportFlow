from __future__ import annotations

from typing import Any

import httpx


class BitrixError(RuntimeError):
    """A sanitized Bitrix error which never contains credentials."""


class BitrixClient:
    def __init__(
        self,
        *,
        webhook_url: str = "",
        gateway_url: str = "",
        gateway_token: str = "",
        gateway_profile: str = "default",
    ):
        if not webhook_url and not gateway_url:
            raise ValueError("Bitrix webhook or gateway must be configured")
        self.webhook_url = webhook_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.gateway_profile = gateway_profile

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.gateway_url:
                    response = await client.post(
                        f"{self.gateway_url}/bitrix/call",
                        headers={"Authorization": f"Bearer {self.gateway_token}"},
                        json={
                            "method": method,
                            "params": params,
                            "profile": self.gateway_profile,
                        },
                    )
                else:
                    response = await client.post(
                        f"{self.webhook_url}/{method}.json",
                        json=params,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise BitrixError("Bitrix is temporarily unavailable") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BitrixError(f"Bitrix returned HTTP {response.status_code}") from exc
        if response.is_error or payload.get("error"):
            message = payload.get("error_description") or payload.get("detail") or payload.get("error")
            raise BitrixError(f"Bitrix call {method} failed: {message}")
        return payload.get("result", payload)

    async def list_active_deals(self, assigned_by_id: int) -> list[dict[str, Any]]:
        deals: list[dict[str, Any]] = []
        start = 0
        while True:
            result = await self.call(
                "crm.deal.list",
                {
                    "filter": {"ASSIGNED_BY_ID": assigned_by_id, "CLOSED": "N"},
                    "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "CATEGORY_ID", "STAGE_ID", "DATE_MODIFY"],
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            page = result["items"] if isinstance(result, dict) and "items" in result else result
            page = page if isinstance(page, list) else []
            deals.extend(page)
            if len(page) < 50:
                break
            start += len(page)
        return deals

    async def get_deal(self, deal_id: int) -> dict[str, Any]:
        result = await self.call("crm.deal.get", {"id": deal_id})
        if not isinstance(result, dict):
            raise BitrixError(f"Deal {deal_id} returned an invalid response")
        return result

    async def set_deal_assignee(self, deal_id: int, user_id: int) -> None:
        result = await self.call(
            "crm.deal.update",
            {"id": deal_id, "fields": {"ASSIGNED_BY_ID": user_id}},
        )
        if result is not True and result != 1:
            raise BitrixError(f"Deal {deal_id} update was not acknowledged")

    async def active_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        start = 0
        while True:
            result = await self.call("user.get", {"FILTER": {"ACTIVE": True}, "start": start})
            page = result if isinstance(result, list) else []
            users.extend(page)
            if len(page) < 50:
                break
            start += len(page)
        return users

    async def get_user(self, user_id: int) -> dict[str, Any]:
        result = await self.call("user.get", {"ID": user_id})
        users = result if isinstance(result, list) else []
        if not users:
            raise BitrixError(f"Bitrix user {user_id} was not found")
        return users[0]
