"""任务提交、查询与取消。"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError, forbidden, invalid_input, not_found
from ..core.security import new_id
from ..core.timeutil import day_window, to_iso, utcnow
from ..models import Task
from ..runtime import Runtime
from ..schemas import (
    CancelResponse,
    CreateTaskRequest,
    MeResponse,
    TaskCreatedResponse,
    TaskError,
    TaskListItem,
    TaskListResponse,
    TaskResponse,
    TaskSource,
    TaskUsage,
)
from ..services.callback import callback_url_allowed
from .deps import CurrentAuth, DbSession, runtime_dep

router = APIRouter(tags=["tasks"])

TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "timeout")
ACTIVE_STATUSES = ("running", "streaming")


async def _count_tasks(db: AsyncSession, user_id: int, statuses: tuple[str, ...]) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Task)
        .where(Task.user_id == user_id, Task.status.in_(statuses))
    )
    return int(result.scalar_one())


async def _used_today(db: AsyncSession, user_id: int, offset_hours: int) -> int:
    start, end = day_window(offset_hours)
    result = await db.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.user_id == user_id,
            Task.created_at >= start,
            Task.created_at < end,
        )
    )
    return int(result.scalar_one())


def _estimated_wait(queue_pos: int, idle_instances: int) -> int:
    pending_ahead = max(0, queue_pos - 1 - max(0, idle_instances - 1))
    return pending_ahead * 60


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise invalid_input("url 必须是合法的 http(s) 地址")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return max(0, int(data.get("offset", 0)))
    except Exception:
        raise invalid_input("cursor 不合法") from None


@router.get("/me", response_model=MeResponse)
async def me(
    auth: CurrentAuth,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> MeResponse:
    settings = runtime.settings
    used = await _used_today(db, auth.user.id, settings.quota_timezone_offset_hours)
    queue_depth = await _count_tasks(db, auth.user.id, ("queued",))
    return MeResponse(
        id=auth.user.id,
        username=auth.user.username,
        display_name=auth.user.display_name,
        daily_quota=auth.user.daily_quota,
        used_today=used,
        remaining_today=max(0, auth.user.daily_quota - used),
        queue_depth=queue_depth,
        in_flight_limit=settings.max_in_flight_per_user,
    )


@router.post("/tasks", response_model=TaskCreatedResponse, status_code=201)
async def create_task(
    payload: CreateTaskRequest,
    auth: CurrentAuth,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key")] = None,
) -> TaskCreatedResponse:
    settings = runtime.settings

    if idempotency_key:
        result = await db.execute(
            select(Task).where(
                Task.user_id == auth.user.id,
                Task.idempotency_key == idempotency_key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return _created_response(existing, settings)

    if payload.kind == "url":
        if not payload.url:
            raise invalid_input("kind=url 时必须提供 url")
        _validate_url(payload.url)
    else:
        if not payload.text or not payload.text.strip():
            raise invalid_input("kind=text 时必须提供 text")
        if len(payload.text) > settings.max_text_chars:
            raise invalid_input(f"text 长度超过上限 {settings.max_text_chars} 字符")

    if payload.callback_url and not callback_url_allowed(
        payload.callback_url, settings.callback_allowed_host_set
    ):
        raise invalid_input("callback_url 必须是白名单域名下的 HTTPS 地址")

    used = await _used_today(db, auth.user.id, settings.quota_timezone_offset_hours)
    if used >= auth.user.daily_quota:
        raise AppError(429, "QUOTA_EXCEEDED", "今日配额已用尽，请明天再试")

    in_flight = await _count_tasks(db, auth.user.id, ACTIVE_STATUSES)
    if in_flight >= settings.max_in_flight_per_user:
        raise AppError(
            429,
            "QUEUE_FULL",
            f"你当前已有 {in_flight} 个任务在处理中，请等待完成或取消",
            retry_after=30,
        )

    queue_depth = await _count_tasks(db, auth.user.id, ("queued",))
    if queue_depth >= settings.max_queue_depth_per_user:
        raise AppError(
            429,
            "QUEUE_FULL",
            f"你的待处理队列已满（{queue_depth} 个），请稍后再试",
            retry_after=60,
        )

    task = Task(
        id=new_id("tsk"),
        user_id=auth.user.id,
        client_task_id=payload.client_task_id,
        idempotency_key=idempotency_key,
        kind=payload.kind,
        input_url=payload.url if payload.kind == "url" else None,
        input_text=payload.text if payload.kind == "text" else None,
        instruction=payload.instruction,
        max_output_chars=payload.max_output_chars,
        status="queued",
        callback_url=payload.callback_url,
        callback_state="none",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    await runtime.bus.emit(task.id, "status", {"status": "queued", "queue_pos": task.queue_pos})
    runtime.dispatcher.wake()

    max_queue = settings.max_queue_depth_per_user
    stats = runtime.pool.stats()
    task.queue_pos = task.queue_pos or 1
    return _created_response(task, settings, idle=stats["idle"], max_queue=max_queue)


def _created_response(
    task: Task, settings, *, idle: int | None = None, max_queue: int | None = None
) -> TaskCreatedResponse:
    position = task.queue_pos or (1 if task.status == "queued" else 0)
    return TaskCreatedResponse(
        task_id=task.id,
        status=task.status,
        queue_pos=position,
        estimated_wait_seconds=_estimated_wait(position, idle if idle is not None else 0),
        created_at=to_iso(task.created_at, settings.quota_timezone_offset_hours) or "",
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    auth: CurrentAuth,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Optional[str] = None,
    status: Optional[str] = None,
) -> TaskListResponse:
    settings = runtime.settings
    offset = _decode_cursor(cursor)
    query = select(Task).where(Task.user_id == auth.user.id)
    if status:
        query = query.where(Task.status == status)
    query = query.order_by(Task.created_at.desc(), Task.id.desc()).offset(offset).limit(limit + 1)
    result = await db.execute(query)
    rows = list(result.scalars().all())

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        TaskListItem(
            task_id=row.id,
            status=row.status,
            kind=row.kind,
            created_at=to_iso(row.created_at, settings.quota_timezone_offset_hours),
            summary_head=(row.result_md or "")[:100] or None,
        )
        for row in rows
    ]
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return TaskListResponse(items=items, next_cursor=next_cursor)


async def _load_owned_task(db: AsyncSession, task_id: str, user_id: int) -> Task:
    task = await db.get(Task, task_id)
    if task is None:
        raise not_found()
    if task.user_id != user_id:
        raise forbidden()
    return task


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    auth: CurrentAuth,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> TaskResponse:
    settings = runtime.settings
    task = await _load_owned_task(db, task_id, auth.user.id)
    if _is_expired(task, settings.result_retention_days):
        raise not_found("任务已过留存期")
    return _to_response(task, settings.quota_timezone_offset_hours)


def _is_expired(task: Task, retention_days: int) -> bool:
    if task.status not in TERMINAL_STATUSES or task.finished_at is None:
        return False
    return utcnow() - task.finished_at > timedelta(days=retention_days)


def _to_response(task: Task, offset_hours: int) -> TaskResponse:
    duration_ms = 0
    if task.started_at and task.finished_at:
        duration_ms = int((task.finished_at - task.started_at).total_seconds() * 1000)
    return TaskResponse(
        task_id=task.id,
        client_task_id=task.client_task_id,
        kind=task.kind,
        status=task.status,
        queue_pos=task.queue_pos or 0,
        result_md=task.result_md,
        source=TaskSource(
            url=task.input_url,
            title=task.source_title,
            fetched_chars=task.fetched_chars,
        ),
        usage=TaskUsage(
            tokens_in=task.tokens_in,
            tokens_out=task.tokens_out,
            duration_ms=duration_ms,
        ),
        error=(
            TaskError(code=task.error_code, message=task.error_message or "")
            if task.error_code
            else None
        ),
        created_at=to_iso(task.created_at, offset_hours),
        started_at=to_iso(task.started_at, offset_hours),
        finished_at=to_iso(task.finished_at, offset_hours),
    )


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    task_id: str,
    auth: CurrentAuth,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> CancelResponse:
    task = await _load_owned_task(db, task_id, auth.user.id)
    if task.status in TERMINAL_STATUSES:
        raise AppError(
            409, "TASK_NOT_CANCELABLE", f"任务已处于终态 {task.status}，无法取消"
        )
    await runtime.dispatcher.cancel(task_id)
    return CancelResponse(task_id=task_id, status="canceled")
