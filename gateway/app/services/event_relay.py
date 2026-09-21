"""每个实例只开一条上游 /event，按 sessionID 扇出到具体任务。

不要为每个用户各开一条上游连接：20 人叠 3 个实例会产生几十条长连接。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .dispatcher import Dispatcher
from .instance_pool import InstancePool, InstanceRuntime

logger = logging.getLogger(__name__)

IGNORED_EVENTS = {"server.connected", "server.heartbeat", "server.instance.disposed"}
RECONNECT_DELAY_SECONDS = 3.0


class EventRelay:
    def __init__(self, pool: InstancePool, dispatcher: Dispatcher) -> None:
        self.pool = pool
        self.dispatcher = dispatcher
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    async def start(self) -> None:
        for instance in self.pool.instances:
            self._tasks.append(asyncio.create_task(self._consume(instance)))
        logger.info("事件流转发已启动：%d 条上游连接", len(self._tasks))

    async def stop(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _consume(self, instance: InstanceRuntime) -> None:
        """断线后自动重连；server.instance.disposed 会正常结束流。"""
        while not self._closed:
            try:
                async for event in instance.client.stream_events():  # type: ignore[attr-defined]
                    await self._handle(instance, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    return
                logger.warning(
                    "实例 %s 事件流中断，%.0fs 后重连: %s",
                    instance.id,
                    RECONNECT_DELAY_SECONDS,
                    exc,
                )
                try:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                except asyncio.CancelledError:
                    raise

    async def _handle(self, instance: InstanceRuntime, event: dict[str, Any]) -> None:
        event_type = event.get("type") or ""
        if not event_type or event_type in IGNORED_EVENTS:
            return
        properties = event.get("properties") or {}

        if event_type == "message.part.updated":
            part = properties.get("part") or {}
            delta = properties.get("delta")
            session_id = part.get("sessionID")
            if delta and part.get("type") == "text" and session_id:
                await self.dispatcher.handle_delta(str(session_id), str(delta))
            return

        if event_type == "message.updated":
            info = properties.get("info") or {}
            session_id = info.get("sessionID")
            if not session_id:
                return
            if info.get("role") == "assistant":
                if info.get("error"):
                    code, message = _describe_error(info["error"])
                    await self.dispatcher.handle_session_error(str(session_id), code, message)
                elif (info.get("time") or {}).get("completed"):
                    await self.dispatcher.handle_usage(str(session_id), info)
            return

        if event_type == "session.idle":
            session_id = properties.get("sessionID")
            if session_id:
                await self.dispatcher.handle_completion(str(session_id))
            return

        if event_type == "session.error":
            session_id = properties.get("sessionID")
            code, message = _describe_error(properties.get("error"))
            if session_id:
                await self.dispatcher.handle_session_error(str(session_id), code, message)
            return

        if event_type == "permission.updated":
            # 正常情况下工具已被 permission 全量 deny，不会走到这里；
            # 一旦出现说明配置被改动，记录以便排查任务卡住的原因。
            logger.warning(
                "收到权限询问（应被配置拦截）instance=%s session=%s",
                instance.id,
                properties.get("sessionID"),
            )
            return


def _describe_error(error: Any) -> tuple[str, str]:
    if isinstance(error, dict):
        name = str(error.get("name") or "UPSTREAM_ERROR")
        data = error.get("data")
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("kind") or name)
        elif data is not None:
            message = str(data)
        else:
            message = name
        if name == "MessageAbortedError":
            return "UPSTREAM_ERROR", "任务已被中止"
        if "OutputLength" in name:
            return "UPSTREAM_ERROR", "输出超出模型长度限制"
        return "UPSTREAM_ERROR", message
    if error is None:
        return "UPSTREAM_ERROR", "智能体返回未知错误"
    return "UPSTREAM_ERROR", str(error)
