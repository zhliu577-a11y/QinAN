"""每个实例只开一条上游 /event，按 sessionID 扇出到具体任务。

不要为每个用户各开一条上游连接：20 人叠 3 个实例会产生几十条长连接。

opencode 1.18 的增量走 `message.part.delta`（properties 里是 partID/field/delta），
而不是挂在 `message.part.updated` 上。这里两种都处理，并额外做一层类型过滤：
reasoning 的增量同样写在 field="text" 上，不过滤就会把模型思维链推给用户。
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
TEXT_PART = "text"
TOOL_PART = "tool"
WEBFETCH_TOOL = "webfetch"
# 只用于记录 partID -> part.type 的映射；超过上限说明有会话没正常收到 session.idle
MAX_TRACKED_PARTS = 4096
# 已计入的 webfetch 调用 ID，避免同一部件在 pending/running/completed 之间反复上报
MAX_TRACKED_CALLS = 4096


class EventRelay:
    def __init__(self, pool: InstancePool, dispatcher: Dispatcher) -> None:
        self.pool = pool
        self.dispatcher = dispatcher
        self._tasks: list[asyncio.Task[None]] = []
        self._part_types: dict[str, str] = {}
        self._parts_by_session: dict[str, set[str]] = {}
        self._seen_fetch_calls: set[str] = set()
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
            session_id = part.get("sessionID") or properties.get("sessionID")
            part_id = part.get("id")
            part_type = str(part.get("type") or "")
            if session_id and part_id:
                self._remember_part(str(session_id), str(part_id), part_type)
            if part_type == TOOL_PART and session_id:
                await self._handle_tool(str(session_id), part)
            # 兼容把增量直接挂在 updated 上的版本
            delta = properties.get("delta")
            if delta and part_type == TEXT_PART and session_id:
                await self.dispatcher.handle_delta(str(session_id), str(delta))
            return

        if event_type == "message.part.delta":
            session_id = properties.get("sessionID")
            part_id = properties.get("partID")
            delta = properties.get("delta")
            if not session_id or not delta or properties.get("field") != "text":
                return
            part_type = self._part_types.get(str(part_id))
            if part_type != TEXT_PART:
                logger.debug(
                    "忽略非文本增量 instance=%s part=%s type=%s",
                    instance.id,
                    part_id,
                    part_type,
                )
                return
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
                self._forget_session(str(session_id))
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

    async def _handle_tool(self, session_id: str, part: dict[str, Any]) -> None:
        """把 webfetch 实际抓到的正文长度记到任务上。

        同一个工具部件会随 pending → running → completed 多次推送，
        且每次都是全量 part，所以只在 completed 时计一次，并按 callID 去重。
        """
        if part.get("tool") != WEBFETCH_TOOL:
            return
        state = part.get("state") or {}
        if state.get("status") != "completed":
            return
        call_id = str(part.get("callID") or part.get("id") or "")
        if not call_id or call_id in self._seen_fetch_calls:
            return
        if len(self._seen_fetch_calls) >= MAX_TRACKED_CALLS:
            logger.warning("webfetch 调用表超过 %d 条，整体清空", MAX_TRACKED_CALLS)
            self._seen_fetch_calls.clear()
        self._seen_fetch_calls.add(call_id)

        output = state.get("output")
        chars = len(output) if isinstance(output, str) else 0
        metadata = state.get("metadata") or {}
        await self.dispatcher.handle_fetch(
            session_id, chars, _clean_fetch_title(state.get("title"))
        )
        logger.debug(
            "webfetch 完成 session=%s chars=%d truncated=%s",
            session_id,
            chars,
            metadata.get("truncated"),
        )

    def _remember_part(self, session_id: str, part_id: str, part_type: str) -> None:
        if not part_type:
            return
        if len(self._part_types) >= MAX_TRACKED_PARTS:
            logger.warning("part 类型表超过 %d 条，整体清空", MAX_TRACKED_PARTS)
            self._part_types.clear()
            self._parts_by_session.clear()
        self._part_types[part_id] = part_type
        self._parts_by_session.setdefault(session_id, set()).add(part_id)

    def _forget_session(self, session_id: str) -> None:
        for part_id in self._parts_by_session.pop(session_id, ()):
            self._part_types.pop(part_id, None)


def _clean_fetch_title(title: Any) -> str | None:
    """webfetch 的 title 形如 "<url> (text/html;charset=UTF-8)"。

    括号里那截是 content-type，不是网页标题，去掉；只剩 URL 时返回 None，
    免得把 URL 重复塞进 source.title。抓取失败时 title 多为 "Fetch failed"，
    这类值直接忽略。
    """
    if not isinstance(title, str):
        return None
    value = title.strip()
    head, sep, tail = value.rpartition(" (")
    if sep and tail.endswith(")") and "/" in tail:
        value = head.strip()
    if not value or value.lower().startswith("fetch failed"):
        return None
    return value


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
