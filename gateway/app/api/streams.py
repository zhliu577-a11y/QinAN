"""流式获取结果：SSE 与 WebSocket。

两者共用同一条订阅逻辑，并支持断线重连补发：
- SSE 读 Last-Event-ID 头或 ?last_seq=
- WebSocket 读首帧 {"type":"attach","last_seq":n}
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import forbidden, not_found
from ..models import Task
from ..runtime import get_runtime
from ..services.events_bus import TERMINAL_EVENT_TYPES, TaskEventBus
from .deps import _extract_bearer, _load_context

router = APIRouter(tags=["streams"])


def _sse_frame(event: dict[str, Any]) -> str:
    payload = {"type": event["type"], **event["data"]}
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


async def _stream_events(
    bus: TaskEventBus, task_id: str, since_seq: int, ping_interval: int
) -> AsyncIterator[str]:
    subscription = bus.subscribe(task_id, since_seq)
    iterator = subscription.__aiter__()
    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    iterator.__anext__(), timeout=ping_interval
                )
            except TimeoutError:
                # 注释帧用于穿透代理的 idle 超时，客户端会忽略
                yield ": ping\n\n"
                continue
            except StopAsyncIteration:
                break
            yield _sse_frame(event)
            if event["type"] in TERMINAL_EVENT_TYPES:
                break
    finally:
        await subscription.aclose()


async def _ensure_owned(
    db: AsyncSession, task_id: str, user_id: int
) -> Task:
    task = await db.get(Task, task_id)
    if task is None:
        raise not_found()
    if task.user_id != user_id:
        raise forbidden()
    return task


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    authorization: Annotated[Optional[str], Header()] = None,
    token: Optional[str] = None,
    last_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[Optional[str], Header(alias="Last-Event-ID")] = None,
):
    runtime = get_runtime()
    since = last_seq
    if last_event_id:
        try:
            since = max(since, int(last_event_id))
        except ValueError:
            pass

    async with runtime.db_session() as db:
        context = await _load_context(db, _extract_bearer(authorization) or token)
        await _ensure_owned(db, task_id, context.user.id)

    return StreamingResponse(
        _stream_events(
            runtime.bus, task_id, since, runtime.settings.sse_ping_interval_seconds
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.websocket("/ws/tasks/{task_id}")
async def task_websocket(websocket: WebSocket, task_id: str) -> None:
    runtime = get_runtime()
    token = websocket.query_params.get("token")
    header_token = _extract_bearer(websocket.headers.get("authorization"))

    try:
        async with runtime.db_session() as db:
            context = await _load_context(db, header_token or token)
            await _ensure_owned(db, task_id, context.user.id)
    except Exception:
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()
    subscription = runtime.bus.subscribe(task_id, 0)
    iterator = subscription.__aiter__()
    ping_interval = runtime.settings.sse_ping_interval_seconds

    async def reader() -> None:
        """持续读取客户端消息，避免接收缓冲区堆积导致连接被判定为僵死。"""
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            return

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=ping_interval)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            except StopAsyncIteration:
                break
            await websocket.send_json({"type": event["type"], **event["data"]})
            if event["type"] in TERMINAL_EVENT_TYPES:
                break
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        await subscription.aclose()
        try:
            await websocket.close()
        except Exception:
            pass
