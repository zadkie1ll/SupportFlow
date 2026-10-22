from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from call_stats_bot.bitrix import BitrixClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmployeeRow:
    extension: str
    user_id: int
    name: str
    active: bool


def _user_name(user: dict) -> str:
    name = " ".join(str(user.get(key, "")).strip() for key in ("NAME", "LAST_NAME")).strip()
    return name or str(user.get("EMAIL") or user.get("ID") or "Сотрудник")


class EmployeeRoster:
    def __init__(
        self,
        bitrix: BitrixClient,
        mapping: dict[str, int],
        refresh_seconds: int = 300,
        name_provider: Callable[[], Awaitable[dict[str, str]]] | None = None,
    ):
        self.bitrix = bitrix
        self.mapping = mapping
        self.refresh_seconds = max(60, refresh_seconds)
        self.name_provider = name_provider
        self._rows: tuple[EmployeeRow, ...] | None = None
        self._lock = asyncio.Lock()

    async def refresh(self) -> tuple[EmployeeRow, ...]:
        async with self._lock:
            active_users, employee_names = await asyncio.gather(
                self.bitrix.active_users(),
                self.name_provider() if self.name_provider else self._empty_names(),
            )
            active_by_id = {
                int(user["ID"]): user
                for user in active_users
                if user.get("ID")
                and str(user.get("ACTIVE", "Y")).strip().upper() not in {"N", "FALSE", "0"}
            }
            rows = [
                EmployeeRow(
                    extension,
                    user_id,
                    employee_names.get(extension) or _user_name(active_by_id[user_id]),
                    True,
                )
                for extension, user_id in sorted(self.mapping.items())
                if user_id in active_by_id
            ]
            inactive = [
                (extension, user_id)
                for extension, user_id in sorted(self.mapping.items())
                if user_id not in active_by_id
            ]
            candidates = await asyncio.gather(
                *(
                    self._inactive_with_deals(
                        extension, user_id, employee_names.get(extension),
                    )
                    for extension, user_id in inactive
                )
            )
            rows.extend(row for row in candidates if row is not None)
            self._rows = tuple(sorted(rows, key=lambda row: row.extension))
            logger.info(
                "Employee roster refreshed: active=%s former_with_deals=%s",
                sum(row.active for row in self._rows),
                sum(not row.active for row in self._rows),
            )
            return self._rows

    async def _inactive_with_deals(
        self, extension: str, user_id: int, employee_name: str | None,
    ) -> EmployeeRow | None:
        if not await self.bitrix.list_active_deals(user_id):
            return None
        user = await self.bitrix.get_user(user_id)
        return EmployeeRow(extension, user_id, employee_name or _user_name(user), False)

    @staticmethod
    async def _empty_names() -> dict[str, str]:
        return {}

    async def rows(self, *, active_only: bool = False) -> tuple[EmployeeRow, ...]:
        rows = self._rows if self._rows is not None else await self.refresh()
        return tuple(row for row in rows if row.active) if active_only else rows

    async def polling_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cannot refresh employee roster from Bitrix")
            await asyncio.sleep(self.refresh_seconds)
