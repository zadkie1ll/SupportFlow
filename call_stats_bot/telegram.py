from __future__ import annotations

import asyncio

import httpx


class TelegramApiError(RuntimeError):
    """Telegram error without the bot-token URL in the exception text."""


def _telegram_timeout(read: float = 15) -> httpx.Timeout:
    return httpx.Timeout(read, connect=5, write=10, pool=5)


async def send_message(
    token: str,
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> int:
    async with httpx.AsyncClient(timeout=_telegram_timeout()) as client:
        response = None
        for attempt in range(4):
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        **({"reply_markup": reply_markup} if reply_markup else {}),
                    },
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == 3:
                    raise TelegramApiError(
                        "Cannot connect to Telegram after 4 attempts"
                    ) from exc
                await asyncio.sleep((2, 5, 10)[attempt])
        assert response is not None
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"Telegram returned HTTP {response.status_code}") from exc
        if not payload.get("ok"):
            description = payload.get("description", "Telegram API error")
            raise TelegramApiError(f"Telegram returned HTTP {response.status_code}: {description}")
        return int((payload.get("result") or {}).get("message_id", 0))


async def edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    response = None
    async with httpx.AsyncClient(timeout=_telegram_timeout(20)) as client:
        for attempt in range(4):
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        **({"reply_markup": reply_markup} if reply_markup else {}),
                    },
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                if attempt == 3:
                    raise TelegramApiError("Cannot update Telegram progress") from exc
                await asyncio.sleep((1, 3, 7)[attempt])
    assert response is not None
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramApiError(f"Telegram returned HTTP {response.status_code}") from exc
    if payload.get("ok"):
        return
    description = str(payload.get("description", "Telegram API error"))
    if "message is not modified" in description.lower():
        return
    raise TelegramApiError(f"Telegram returned HTTP {response.status_code}: {description}")


async def answer_callback_query(token: str, callback_query_id: str, text: str = "") -> None:
    try:
        async with httpx.AsyncClient(timeout=_telegram_timeout(8)) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    **({"text": text[:200]} if text else {}),
                },
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise TelegramApiError("Cannot answer Telegram callback") from exc
    if response.is_error:
        raise TelegramApiError(f"Telegram callback returned HTTP {response.status_code}")


async def get_updates(
    token: str,
    offset: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    if client is None:
        async with httpx.AsyncClient(timeout=_telegram_timeout(35)) as temporary_client:
            return await get_updates(token, offset, temporary_client)
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/getUpdates",
            json=params,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise TelegramApiError("Cannot poll Telegram") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramApiError(f"Telegram returned HTTP {response.status_code}") from exc
    if not payload.get("ok"):
        raise TelegramApiError(
            f"Telegram returned HTTP {response.status_code}: {payload.get('description', 'API error')}"
        )
    result = payload.get("result", [])
    return result if isinstance(result, list) else []
