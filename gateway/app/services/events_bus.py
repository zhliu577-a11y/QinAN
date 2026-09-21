"""任务事件总线：落库 + 进程内扇出。

落库是为了让移动端断线重连后能补发（Last-Event-ID / last_seq），
扇出是为了当前在线的 SSE / WebSocket 连接能实时收到增量。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import func, select

from ..core.db import get_sessionmaker
from ..models import TaskEvent

TERMINAL_EVENT_TYPES = {"done", "error"}


class TaskEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._seq: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def _next_seq(self, task_id: str) -> int:
        current = self._seq.get(task_id)
        if current is not None:
            return current + 1
        async with get_sessionmaker()() as db:
            result = await db.execute(
                select(func.coalesce(func.max(TaskEvent.seq), 0)).where(
                    TaskEvent.task_id == task_id
                )
            )
            return int(result.scalar_one()) + 1

    async def emit(
        self, task_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """写入一条事件并推给在线订阅者，返回带 seq 的事件。"""
        async with self._lock_for(task_id):
            seq = await self._next_seq(task_id)
            self._seq[task_id] = seq
            body = payload or {}
            async with get_sessionmaker()() as db:
                db.add(
                    TaskEvent(
                        task_id=task_id,
                        seq=seq,
                        type=event_type,
                        payload=json.dumps(body, ensure_ascii=False),
                    )
                )
                await db.commit()

        event = {"seq": seq, "type": event_type, "data": body}
        for queue in list(self._subscribers.get(task_id, ())):
            queue.put_nowait(event)
        return event

    async def _load_since(self, task_id: str, since_seq: int) -> list[dict[str, Any]]:
        async with get_sessionmaker()() as db:
            result = await db.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.seq > since_seq)
                .order_by(TaskEvent.seq)
            )
            rows = result.scalars().all()
        return [
            {"seq": row.seq, "type": row.type, "data": json.loads(row.payload or "{}")}
            for row in rows
        ]

    async def subscribe(
        self, task_id: str, since_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """先补发历史事件，再转入实时推送；按 seq 去重防止重复。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # 先注册再补发，避免补发与推送之间出现丢事件的窗口
        self._subscribers.setdefault(task_id, set()).add(queue)
        try:
            last_seq = since_seq
            for event in await self._load_since(task_id, since_seq):
                last_seq = event["seq"]
                yield event
            while True:
                event = await queue.get()
                if event["seq"] <= last_seq:
                    continue
                last_seq = event["seq"]
                yield event
        finally:
            subscribers = self._subscribers.get(task_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(task_id, None)

    def forget(self, task_id: str) -> None:
        self._seq.pop(task_id, None)
        self._locks.pop(task_id, None)
