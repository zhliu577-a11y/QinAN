"""调度器：实例池 + 用户粘性 + 双队列 + 背压 + 超时回收。

派发规则（每 tick 执行一次）：
1. 每用户同一时刻只允许 1 个在飞任务，防止一个人占满整个池；
2. 优先把任务派发到该用户的粘性实例上，命中即可复用会话上下文；
3. 粘性实例不可用时溢出到其他空闲实例，并把上一轮摘要回灌进 prompt；
4. 增量文本先在内聚合成批，再落库与推送，避免每个 token 写一行。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..core.config import Settings
from ..core.db import get_sessionmaker
from ..core.timeutil import utcnow
from ..models import Task, UserBinding
from .callback import CallbackDelivery
from .events_bus import TaskEventBus
from .instance_pool import InstancePool, InstanceRuntime
from .prompting import build_prompt

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("running", "streaming")
TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "timeout")


class Dispatcher:
    def __init__(
        self, pool: InstancePool, bus: TaskEventBus, settings: Settings
    ) -> None:
        self.pool = pool
        self.bus = bus
        self.settings = settings
        self.callbacks = CallbackDelivery(settings)
        self._buffers: dict[str, list[str]] = {}
        self._pending: dict[str, str] = {}
        self._usage: dict[str, dict[str, Any]] = {}
        self._streaming: set[str] = set()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        await self.callbacks.drain()

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        interval = self.settings.dispatcher_interval_seconds
        while not self._closed:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("调度器 tick 异常")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._wake.clear()

    # ---------- 一个调度周期 ----------

    async def _tick(self) -> None:
        await self._flush_streams()
        await self._requeue_dropped()
        await self._reap_timeouts()
        await self._dispatch_pending()
        await self._refresh_queue_positions()

    async def _flush_streams(self) -> None:
        if not self._pending:
            return
        pending = {task_id: text for task_id, text in self._pending.items() if text}
        self._pending.clear()
        for task_id, text in pending.items():
            if task_id not in self._streaming:
                self._streaming.add(task_id)
                await self._set_status(task_id, "streaming")
            await self.bus.emit(task_id, "delta", {"text": text})

    async def _requeue_dropped(self) -> None:
        dropped = self.pool.drain_dropped()
        if not dropped:
            return
        to_emit: list[tuple[str, str]] = []
        async with get_sessionmaker()() as db:
            for task_id in dropped:
                task = await db.get(Task, task_id)
                if task is None or task.status not in ACTIVE_STATUSES:
                    continue
                self._buffers.pop(task_id, None)
                self._pending.pop(task_id, None)
                self._usage.pop(task_id, None)
                self._streaming.discard(task_id)
                if task.retried:
                    task.status = "failed"
                    task.error_code = "UPSTREAM_ERROR"
                    task.error_message = "智能体实例异常，自动重试后仍然失败"
                    task.finished_at = utcnow()
                    to_emit.append((task_id, "failed"))
                    continue
                task.retried = True
                task.status = "queued"
                task.instance_id = None
                task.session_id = None
                task.started_at = None
                to_emit.append((task_id, "queued"))
            await db.commit()

        for task_id, status in to_emit:
            if status == "queued":
                await self.bus.emit(task_id, "status", {"status": "queued"})
            else:
                await self._emit_terminal(task_id, "failed")

    async def _reap_timeouts(self) -> None:
        deadline = utcnow() - timedelta(seconds=self.settings.task_timeout_seconds)
        async with get_sessionmaker()() as db:
            result = await db.execute(
                select(Task).where(
                    Task.status.in_(ACTIVE_STATUSES),
                    Task.started_at.is_not(None),
                    Task.started_at < deadline,
                )
            )
            expired = list(result.scalars().all())

        for task in expired:
            logger.warning("任务超时 task=%s session=%s", task.id, task.session_id)
            if task.session_id:
                instance = self.pool.get(task.instance_id or "")
                if instance is not None:
                    await instance.client.abort(task.session_id)  # type: ignore[attr-defined]
            await self._finalize(
                task.id,
                "timeout",
                error_code="TIMEOUT",
                error_message=f"任务超过 {self.settings.task_timeout_seconds} 秒未完成",
            )

    async def _dispatch_pending(self) -> None:
        idle = self.pool.idle_instances()
        if not idle:
            return
        available = {instance.id: instance for instance in idle}

        async with get_sessionmaker()() as db:
            inflight = await db.execute(
                select(Task.user_id).where(Task.status.in_(ACTIVE_STATUSES)).distinct()
            )
            busy_users = {int(row) for row in inflight.scalars().all()}

            queued_rows = await db.execute(
                select(Task).where(Task.status == "queued").order_by(Task.created_at, Task.id)
            )
            queued = list(queued_rows.scalars().all())

            binding_rows = await db.execute(select(UserBinding))
            bindings = {row.user_id: row for row in binding_rows.scalars().all()}

        # 每用户只取队首，实现公平闸门
        candidates: list[Task] = []
        seen_users: set[int] = set()
        for task in queued:
            if task.user_id in busy_users or task.user_id in seen_users:
                continue
            seen_users.add(task.user_id)
            candidates.append(task)

        # 粘性实例当前空闲的任务优先派发，其余按入队顺序
        sticky: list[Task] = []
        rest: list[Task] = []
        for task in candidates:
            binding = bindings.get(task.user_id)
            if binding is not None and binding.instance_id in available:
                sticky.append(task)
            else:
                rest.append(task)

        for task in sticky + rest:
            if not available:
                break
            binding = bindings.get(task.user_id)
            instance: InstanceRuntime | None = None
            if binding is not None and binding.instance_id in available:
                instance = available.pop(binding.instance_id)
            else:
                instance = next(iter(available.values()))
                available.pop(instance.id)
            await self._start(task, instance, binding)

    async def _start(
        self, task: Task, instance: InstanceRuntime, binding: UserBinding | None
    ) -> None:
        reuse_session = (
            binding.session_id
            if binding is not None and binding.instance_id == instance.id
            else None
        )
        overflow = binding is not None and binding.instance_id != instance.id
        previous_summary = binding.summary if overflow and binding else None

        prompt = build_prompt(
            kind=task.kind,
            url=task.input_url,
            text=task.input_text,
            instruction=task.instruction,
            max_output_chars=task.max_output_chars,
            max_fetch_chars=self.settings.max_fetch_chars,
            max_text_chars=self.settings.max_text_chars,
            previous_summary=previous_summary,
        )

        try:
            session_id = reuse_session or await instance.client.create_session(  # type: ignore[attr-defined]
                title=f"{task.kind}-{task.id}"
            )
        except Exception as exc:
            logger.warning("创建会话失败 instance=%s: %s", instance.id, exc)
            await self._defer(task.id)
            return

        # 先登记会话映射，再下发，避免事件到达时找不到任务
        instance.sessions[session_id] = task.id
        self.pool.mark_busy(instance, task.id)
        self._buffers[task.id] = []
        self._usage[task.id] = {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}

        async with get_sessionmaker()() as db:
            row = await db.get(Task, task.id)
            if row is not None:
                row.status = "running"
                row.instance_id = instance.id
                row.session_id = session_id
                row.started_at = utcnow()
                row.queue_pos = 0
            existing = await db.get(UserBinding, task.user_id)
            if existing is None:
                db.add(
                    UserBinding(
                        user_id=task.user_id,
                        instance_id=instance.id,
                        session_id=session_id,
                    )
                )
            else:
                existing.instance_id = instance.id
                existing.session_id = session_id
                existing.last_used_at = utcnow()
            await db.commit()

        try:
            await instance.client.prompt_async(  # type: ignore[attr-defined]
                session_id,
                prompt,
                agent=self.settings.summarizer_agent,
                provider_id=self.settings.model_provider,
                model_id=self.settings.model_name,
            )
        except Exception as exc:
            logger.error("下发任务失败 instance=%s task=%s: %s", instance.id, task.id, exc)
            instance.sessions.pop(session_id, None)
            self.pool.release(instance)
            self._buffers.pop(task.id, None)
            self._usage.pop(task.id, None)
            await self._defer(task.id)
            return

        await self.bus.emit(task.id, "status", {"status": "running"})

    async def _defer(self, task_id: str) -> None:
        """下发失败时把任务放回队列，最多重试一次。"""
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            if task.retried:
                task.status = "failed"
                task.error_code = "UPSTREAM_ERROR"
                task.error_message = "无法把任务下发给智能体实例"
                task.finished_at = utcnow()
                await db.commit()
                await self._emit_terminal(task_id, "failed")
                return
            task.retried = True
            task.status = "queued"
            task.instance_id = None
            task.session_id = None
            task.started_at = None
            await db.commit()
        await self.bus.emit(task_id, "status", {"status": "queued"})

    async def _refresh_queue_positions(self) -> None:
        async with get_sessionmaker()() as db:
            result = await db.execute(
                select(Task).where(Task.status == "queued").order_by(Task.created_at, Task.id)
            )
            queued = list(result.scalars().all())
            counters: dict[int, int] = {}
            changed: list[tuple[str, int]] = []
            for task in queued:
                counters[task.user_id] = counters.get(task.user_id, 0) + 1
                position = counters[task.user_id]
                if task.queue_pos != position:
                    task.queue_pos = position
                    changed.append((task.id, position))
            if changed:
                await db.commit()

        for task_id, position in changed:
            await self.bus.emit(task_id, "status", {"status": "queued", "queue_pos": position})

    # ---------- 来自事件流的回调 ----------

    async def handle_delta(self, session_id: str, text: str) -> None:
        found = self.pool.task_of_session(session_id)
        if found is None or not text:
            return
        _, task_id = found
        self._buffers.setdefault(task_id, []).append(text)
        self._pending[task_id] = self._pending.get(task_id, "") + text
        self.wake()

    async def handle_usage(self, session_id: str, info: dict[str, Any]) -> None:
        found = self.pool.task_of_session(session_id)
        if found is None:
            return
        _, task_id = found
        tokens = info.get("tokens") or {}
        usage = self._usage.setdefault(
            task_id, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}
        )
        usage["tokens_in"] = int(tokens.get("input") or usage["tokens_in"])
        usage["tokens_out"] = int(tokens.get("output") or usage["tokens_out"])
        usage["cost"] = float(info.get("cost") or usage["cost"])

    async def handle_completion(self, session_id: str) -> None:
        found = self.pool.task_of_session(session_id)
        if found is None:
            return
        instance, task_id = found
        await self._flush_task(task_id)
        text = "".join(self._buffers.get(task_id, []))

        if not text:
            fallback = await instance.client.last_assistant_message(session_id)  # type: ignore[attr-defined]
            if fallback:
                text = fallback.get("text") or ""
                await self.handle_usage(session_id, fallback.get("info") or {})

        if not text:
            await self._finalize(
                task_id,
                "failed",
                error_code="UPSTREAM_ERROR",
                error_message="智能体没有返回任何内容",
            )
            return

        await self._finalize(task_id, "succeeded", result_md=text)

    async def handle_session_error(
        self, session_id: str, code: str, message: str
    ) -> None:
        found = self.pool.task_of_session(session_id)
        if found is None:
            return
        _, task_id = found
        await self._finalize(task_id, "failed", error_code=code, error_message=message)

    async def _flush_task(self, task_id: str) -> None:
        text = self._pending.pop(task_id, "")
        if text:
            if task_id not in self._streaming:
                self._streaming.add(task_id)
                await self._set_status(task_id, "streaming")
            await self.bus.emit(task_id, "delta", {"text": text})

    # ---------- 状态落库 ----------

    async def _set_status(self, task_id: str, status: str) -> None:
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is not None and task.status not in TERMINAL_STATUSES:
                task.status = status
                await db.commit()

    async def _finalize(
        self,
        task_id: str,
        status: str,
        *,
        result_md: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is None or task.status in TERMINAL_STATUSES:
                return
            instance_id = task.instance_id
            session_id = task.session_id
            user_id = task.user_id

            task.status = status
            task.finished_at = utcnow()
            task.queue_pos = 0
            if result_md is not None:
                task.result_md = result_md
            task.error_code = error_code
            task.error_message = error_message
            usage = self._usage.pop(task_id, None)
            if usage:
                task.tokens_in = usage["tokens_in"]
                task.tokens_out = usage["tokens_out"]
                task.cost = usage["cost"]
            if task.kind == "text" and task.fetched_chars is None:
                task.fetched_chars = len(task.input_text or "")

            if status == "succeeded" and result_md:
                binding = await db.get(UserBinding, user_id)
                if binding is not None:
                    binding.summary = result_md[:600]
                    binding.last_used_at = utcnow()
            await db.commit()

        self._buffers.pop(task_id, None)
        self._pending.pop(task_id, None)
        self._streaming.discard(task_id)

        if instance_id and session_id:
            instance = self.pool.get(instance_id)
            if instance is not None:
                instance.sessions.pop(session_id, None)
                self.pool.release(instance)

        await self._emit_terminal(task_id, status, error_code, error_message)
        self.bus.forget(task_id)

    async def _emit_terminal(
        self,
        task_id: str,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status == "succeeded":
            await self.bus.emit(task_id, "done", {"status": status})
        else:
            await self.bus.emit(
                task_id,
                "error",
                {
                    "status": status,
                    "error": {"code": error_code or "UPSTREAM_ERROR", "message": error_message or ""},
                },
            )
        self.callbacks.schedule(task_id)

    # ---------- 取消 ----------

    async def cancel(self, task_id: str) -> str:
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return "missing"
            if task.status in TERMINAL_STATUSES:
                return task.status
            instance_id = task.instance_id
            session_id = task.session_id

        if instance_id and session_id:
            instance = self.pool.get(instance_id)
            if instance is not None:
                await instance.client.abort(session_id)  # type: ignore[attr-defined]

        await self._finalize(task_id, "canceled")
        return "canceled"
