from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from call_stats_bot.bitrix import BitrixClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Substitution:
    id: str
    source_user_id: int
    target_user_id: int
    source_name: str
    target_name: str
    source_extension: str
    target_extension: str
    ends_at: datetime
    status: str
    created_by: int
    progress_chat_id: int | None
    progress_message_id: int | None


@dataclass(frozen=True)
class TelegramWizard:
    admin_id: int
    chat_id: int
    state: str
    source_extension: str
    ends_at: datetime | None


class SubstitutionStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS substitutions (
                    id TEXT PRIMARY KEY,
                    source_user_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    source_name TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    source_extension TEXT NOT NULL DEFAULT '',
                    target_extension TEXT NOT NULL DEFAULT '',
                    ends_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('draft','active','restoring','completed','attention','cancelled')),
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    completed_at TEXT,
                    progress_chat_id INTEGER,
                    progress_message_id INTEGER,
                    CHECK(source_user_id <> target_user_id)
                );
                CREATE TABLE IF NOT EXISTS substitution_deals (
                    substitution_id TEXT NOT NULL REFERENCES substitutions(id),
                    deal_id INTEGER NOT NULL,
                    deal_title TEXT NOT NULL DEFAULT '',
                    original_user_id INTEGER NOT NULL,
                    replacement_user_id INTEGER NOT NULL,
                    observed_date_modify TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK(state IN
                        ('planned','applied','restored','conflict','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    applied_at TEXT,
                    restored_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(substitution_id, deal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_substitutions_status_ends
                    ON substitutions(status, ends_at);
                CREATE INDEX IF NOT EXISTS idx_substitution_deals_state
                    ON substitution_deals(substitution_id, state);
                CREATE TABLE IF NOT EXISTS telegram_wizards (
                    admin_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    source_extension TEXT NOT NULL DEFAULT '',
                    ends_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(substitutions)").fetchall()
            }
            if "source_extension" not in columns:
                db.execute(
                    "ALTER TABLE substitutions ADD COLUMN source_extension TEXT NOT NULL DEFAULT ''"
                )
            if "target_extension" not in columns:
                db.execute(
                    "ALTER TABLE substitutions ADD COLUMN target_extension TEXT NOT NULL DEFAULT ''"
                )
            if "progress_chat_id" not in columns:
                db.execute("ALTER TABLE substitutions ADD COLUMN progress_chat_id INTEGER")
            if "progress_message_id" not in columns:
                db.execute("ALTER TABLE substitutions ADD COLUMN progress_message_id INTEGER")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _substitution(row: sqlite3.Row) -> Substitution:
        return Substitution(
            id=row["id"],
            source_user_id=int(row["source_user_id"]),
            target_user_id=int(row["target_user_id"]),
            source_name=row["source_name"],
            target_name=row["target_name"],
            source_extension=row["source_extension"],
            target_extension=row["target_extension"],
            ends_at=datetime.fromisoformat(row["ends_at"]),
            status=row["status"],
            created_by=int(row["created_by"]),
            progress_chat_id=(
                int(row["progress_chat_id"]) if row["progress_chat_id"] is not None else None
            ),
            progress_message_id=(
                int(row["progress_message_id"]) if row["progress_message_id"] is not None else None
            ),
        )

    def create_draft(
        self, *, source_user_id: int, target_user_id: int, source_name: str,
        target_name: str, source_extension: str, target_extension: str,
        ends_at: datetime, created_by: int
    ) -> Substitution:
        if ends_at.tzinfo is None:
            raise ValueError("ends_at must be timezone-aware")
        operation_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "UPDATE substitutions SET status='cancelled' WHERE status='draft' AND ends_at <= ?",
                (self._now(),),
            )
            overlap = db.execute(
                """
                SELECT id FROM substitutions
                WHERE status IN ('draft','active','restoring')
                  AND (source_user_id IN (?, ?) OR target_user_id IN (?, ?))
                LIMIT 1
                """,
                (source_user_id, target_user_id, source_user_id, target_user_id),
            ).fetchone()
            if overlap:
                raise ValueError(f"Employee already participates in operation {overlap['id'][:8]}")
            db.execute(
                """
                INSERT INTO substitutions(
                    id, source_user_id, target_user_id, source_name, target_name,
                    source_extension, target_extension, ends_at, status, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    operation_id, source_user_id, target_user_id, source_name, target_name,
                    source_extension, target_extension,
                    ends_at.astimezone(timezone.utc).isoformat(), created_by, self._now(),
                ),
            )
        return self.get(operation_id)

    def cancel_draft(self, operation_id: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE substitutions SET status='cancelled' WHERE id=? AND status='draft'",
                (operation_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError("Only a draft can be cancelled")

    def start_wizard(self, admin_id: int, chat_id: int) -> TelegramWizard:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO telegram_wizards(admin_id, chat_id, state, source_extension, ends_at, updated_at)
                VALUES (?, ?, 'choose_source', '', NULL, ?)
                ON CONFLICT(admin_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    state='choose_source',
                    source_extension='',
                    ends_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (admin_id, chat_id, self._now()),
            )
        return self.get_wizard(admin_id)

    def get_wizard(self, admin_id: int) -> TelegramWizard:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM telegram_wizards WHERE admin_id=?", (admin_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Начните новое замещение кнопкой «Создать замещение»")
        return TelegramWizard(
            admin_id=int(row["admin_id"]),
            chat_id=int(row["chat_id"]),
            state=row["state"],
            source_extension=row["source_extension"],
            ends_at=datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else None,
        )

    def set_wizard_source(self, admin_id: int, extension: str) -> TelegramWizard:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE telegram_wizards
                SET source_extension=?, state='choose_date', ends_at=NULL, updated_at=?
                WHERE admin_id=?
                """,
                (extension, self._now(), admin_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Сессия выбора не найдена")
        return self.get_wizard(admin_id)

    def await_wizard_date(self, admin_id: int) -> TelegramWizard:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE telegram_wizards SET state='await_date', updated_at=?
                WHERE admin_id=? AND source_extension<>''
                """,
                (self._now(), admin_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Сначала выберите сотрудника")
        return self.get_wizard(admin_id)

    def set_wizard_date(self, admin_id: int, ends_at: datetime) -> TelegramWizard:
        if ends_at.tzinfo is None:
            raise ValueError("ends_at must be timezone-aware")
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE telegram_wizards
                SET ends_at=?, state='choose_target', updated_at=?
                WHERE admin_id=? AND source_extension<>''
                """,
                (ends_at.astimezone(timezone.utc).isoformat(), self._now(), admin_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Сначала выберите сотрудника")
        return self.get_wizard(admin_id)

    def clear_wizard(self, admin_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM telegram_wizards WHERE admin_id=?", (admin_id,))

    def get(self, operation_id_or_prefix: str) -> Substitution:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM substitutions WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2",
                (f"{operation_id_or_prefix}%",),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("Operation not found or prefix is ambiguous")
        return self._substitution(rows[0])

    def set_status(self, operation_id: str, status: str) -> None:
        extra = ", confirmed_at=?" if status == "active" else ", completed_at=?" if status == "completed" else ""
        params: list[object] = [status]
        if extra:
            params.append(self._now())
        params.append(operation_id)
        with self._connect() as db:
            db.execute(f"UPDATE substitutions SET status=?{extra} WHERE id=?", params)

    def set_progress_message(self, operation_id: str, chat_id: int, message_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE substitutions
                SET progress_chat_id=?, progress_message_id=?
                WHERE id=?
                """,
                (chat_id, message_id, operation_id),
            )

    def record_deal(self, operation: Substitution, deal: dict) -> bool:
        now = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO substitution_deals(
                    substitution_id, deal_id, deal_title, original_user_id,
                    replacement_user_id, observed_date_modify, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)
                """,
                (
                    operation.id, int(deal["ID"]), str(deal.get("TITLE", "")),
                    int(deal["ASSIGNED_BY_ID"]), operation.target_user_id,
                    str(deal.get("DATE_MODIFY", "")), now,
                ),
            )
        return cursor.rowcount == 1

    def deal_rows(self, operation_id: str, states: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM substitution_deals WHERE substitution_id=?"
        params: list[object] = [operation_id]
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            params.extend(states)
        query += " ORDER BY deal_id"
        with self._connect() as db:
            return db.execute(query, params).fetchall()

    def mark_deal(self, operation_id: str, deal_id: int, state: str, error: str = "") -> None:
        now = self._now()
        timestamp_column = ", applied_at=?" if state == "applied" else ", restored_at=?" if state == "restored" else ""
        params: list[object] = [state, error[:1000], now]
        if timestamp_column:
            params.append(now)
        params.extend([operation_id, deal_id])
        with self._connect() as db:
            db.execute(
                f"""
                UPDATE substitution_deals
                SET state=?, last_error=?, attempts=attempts+1, updated_at=?{timestamp_column}
                WHERE substitution_id=? AND deal_id=?
                """,
                params,
            )

    def active(self) -> list[Substitution]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM substitutions WHERE status IN ('active','restoring') ORDER BY ends_at"
            ).fetchall()
        return [self._substitution(row) for row in rows]

    def recent(self, limit: int = 10) -> list[Substitution]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM substitutions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._substitution(row) for row in rows]

    def counts(self, operation_id: str) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS amount FROM substitution_deals WHERE substitution_id=? GROUP BY state",
                (operation_id,),
            ).fetchall()
        return {row["state"]: int(row["amount"]) for row in rows}


class SubstitutionService:
    def __init__(self, store: SubstitutionStore, bitrix: BitrixClient):
        self.store = store
        self.bitrix = bitrix
        self._lock = asyncio.Lock()
        self.batch_size = 20
        self.concurrency = 5

    async def preview(self, operation: Substitution) -> int:
        deals = await self.bitrix.list_active_deals(operation.source_user_id)
        for deal in deals:
            self.store.record_deal(operation, deal)
        return len(self.store.deal_rows(operation.id))

    async def activate(self, operation: Substitution) -> None:
        if operation.status != "draft":
            raise ValueError("Only a draft can be confirmed")
        if operation.ends_at <= datetime.now(timezone.utc):
            raise ValueError("Return time has already passed")
        self.store.set_status(operation.id, "active")

    async def reconcile(self, operation: Substitution) -> None:
        async with self._lock:
            if operation.status == "active" and operation.ends_at <= datetime.now(timezone.utc):
                self.store.set_status(operation.id, "restoring")
                operation = self.store.get(operation.id)
            if operation.status == "active":
                await self._discover_and_apply(operation)
            elif operation.status == "restoring":
                await self._restore(operation)

    async def _discover_and_apply(self, operation: Substitution) -> None:
        for deal in await self.bitrix.list_active_deals(operation.source_user_id):
            self.store.record_deal(operation, deal)
        rows = self.store.deal_rows(operation.id, ("planned", "failed"))[: self.batch_size]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded_apply(row: sqlite3.Row) -> None:
            async with semaphore:
                await self._apply_one(operation, row)

        await asyncio.gather(*(guarded_apply(row) for row in rows))

    async def _apply_one(self, operation: Substitution, row: sqlite3.Row) -> None:
        try:
            current = await self.bitrix.get_deal(int(row["deal_id"]))
            owner = int(current["ASSIGNED_BY_ID"])
            if owner == operation.target_user_id:
                self.store.mark_deal(operation.id, int(row["deal_id"]), "applied")
                return
            if owner != int(row["original_user_id"]):
                self.store.mark_deal(
                    operation.id, int(row["deal_id"]), "conflict",
                    f"Owner changed to {owner} before replacement",
                )
                return
            await self.bitrix.set_deal_assignee(int(row["deal_id"]), operation.target_user_id)
            verified = await self.bitrix.get_deal(int(row["deal_id"]))
            if int(verified["ASSIGNED_BY_ID"]) != operation.target_user_id:
                raise RuntimeError("Bitrix did not persist the replacement")
            self.store.mark_deal(operation.id, int(row["deal_id"]), "applied")
        except Exception as exc:
            logger.exception("Cannot apply substitution %s to deal %s", operation.id, row["deal_id"])
            self.store.mark_deal(operation.id, int(row["deal_id"]), "failed", str(exc))

    async def start_restore(self, operation: Substitution) -> None:
        if operation.status not in {"active", "attention"}:
            raise ValueError("Operation cannot be restored from its current state")
        self.store.set_status(operation.id, "restoring")

    async def _restore(self, operation: Substitution) -> None:
        rows = self.store.deal_rows(operation.id, ("applied", "failed"))[: self.batch_size]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded_restore(row: sqlite3.Row) -> None:
            async with semaphore:
                await self._restore_one(operation, row)

        await asyncio.gather(*(guarded_restore(row) for row in rows))
        pending = self.store.deal_rows(operation.id, ("applied", "failed", "planned"))
        if pending:
            return
        conflicts = self.store.deal_rows(operation.id, ("conflict",))
        self.store.set_status(operation.id, "attention" if conflicts else "completed")

    async def _restore_one(self, operation: Substitution, row: sqlite3.Row) -> None:
        try:
            current = await self.bitrix.get_deal(int(row["deal_id"]))
            owner = int(current["ASSIGNED_BY_ID"])
            original = int(row["original_user_id"])
            if owner == original:
                self.store.mark_deal(operation.id, int(row["deal_id"]), "restored")
                return
            if owner != int(row["replacement_user_id"]):
                self.store.mark_deal(
                    operation.id, int(row["deal_id"]), "conflict",
                    f"Current owner is {owner}; automatic restore skipped",
                )
                return
            await self.bitrix.set_deal_assignee(int(row["deal_id"]), original)
            verified = await self.bitrix.get_deal(int(row["deal_id"]))
            if int(verified["ASSIGNED_BY_ID"]) != original:
                raise RuntimeError("Bitrix did not persist the restore")
            self.store.mark_deal(operation.id, int(row["deal_id"]), "restored")
        except Exception as exc:
            logger.exception("Cannot restore deal %s for substitution %s", row["deal_id"], operation.id)
            self.store.mark_deal(operation.id, int(row["deal_id"]), "failed", str(exc))
