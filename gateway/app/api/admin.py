"""管理接口，只在 internal 网络内可达。

nginx 对 `/internal/` 是 `deny all`：端口经 docker 发布后 $remote_addr 是网桥网关
而不是真实客户端，所以「只允许某个来源网段」在 nginx 层表达不了，干脆完全不对公网
暴露。调用方式见 deploy/README.md：`docker compose exec gateway curl ...`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import ping_db
from ..core.errors import invalid_input, not_found
from ..core.logbuffer import LOG_BUFFER, resolve_level
from ..core.security import hash_password
from ..core.timeutil import day_window, to_iso
from ..models import ApiClient, Task, User, UserBinding
from ..runtime import Runtime
from ..schemas import (
    AdminCreateUserRequest,
    AdminCreateUserResponse,
    AdminUpdateUserRequest,
)
from .deps import DbSession, require_admin_token, runtime_dep

APP_VERSION = "1.0.0"

router = APIRouter(
    prefix="/internal",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


async def _count_tasks_in(
    db: AsyncSession, statuses: tuple[str, ...] | None = None, **where: object
) -> int:
    stmt = select(func.count()).select_from(Task)
    if statuses is not None:
        stmt = stmt.where(Task.status.in_(statuses))
    for column, value in where.items():
        stmt = stmt.where(getattr(Task, column) == value)
    return int((await db.execute(stmt)).scalar_one())


@router.get("/status")
async def status(
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> dict[str, object]:
    """一屏自检：网关是否正常、各依赖是否通、池子和队列什么状况。

    `checks` 里每项都是「名字 + 是否通过 + 说明」，调用方不需要自己解释数字。
    """
    settings = runtime.settings
    stats = runtime.pool.stats()
    db_ok = await ping_db()

    queue_len = 0
    active = 0
    if db_ok:
        queue_len = await _count_tasks_in(db, ("queued",))
        active = await _count_tasks_in(db, ("running", "streaming"))

    engine = runtime.engine_state()
    checks = [
        {
            "name": "runtime_started",
            "ok": bool(runtime.started),
            "detail": f"uptime={runtime.uptime_seconds()}s",
        },
        {"name": "database", "ok": db_ok, "detail": "SELECT 1"},
        {
            "name": "engine",
            "ok": engine == "ready",
            "detail": f"{engine} (healthy={stats['healthy']}/{stats['total']})",
        },
    ]
    return {
        "status": "ok" if all(c["ok"] for c in checks) else "degraded",
        "gateway": {
            "ready": runtime.ready,
            "uptime_seconds": runtime.uptime_seconds(),
            "version": APP_VERSION,
        },
        "mode": "mock" if settings.mock_mode else "real",
        "engine": engine,
        "checks": checks,
        "pool": stats,
        "queue_len": queue_len,
        "active_tasks": active,
        "config": {
            "model_provider": settings.model_provider,
            "model_name": settings.model_name,
            "opencode_instances": len(settings.instance_specs),
            "default_daily_quota": settings.default_daily_quota,
            "max_in_flight_per_user": settings.max_in_flight_per_user,
            "max_queue_depth_per_user": settings.max_queue_depth_per_user,
            "result_retention_days": settings.result_retention_days,
        },
    }


@router.get("/instances")
async def instances(
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> dict[str, object]:
    """后端 opencode 进程明细：谁在忙、忙多久、失败几次、挂了几个会话。

    排查「任务卡住」时先看这里：`status=busy` 且 `busy_seconds` 很大，
    通常是对应进程卡死或模型端不返回。
    """
    return {
        "pool": runtime.pool.stats(),
        "items": runtime.pool.snapshot(),
    }


@router.get("/logs")
async def logs(
    runtime: Annotated[Runtime, Depends(runtime_dep)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    level: Annotated[str | None, Query()] = None,
    logger_name: Annotated[str | None, Query()] = None,
    task_id: Annotated[str | None, Query()] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, object]:
    """网关进程自己的近期日志（环形缓冲，重启即清空）。

    只覆盖网关；nginx / opencode 的日志在容器 stdout 里，用 `deploy/ops.sh logs`
    取。之所以不在这里统一读，是因为那需要把 docker socket 挂进容器。

    增量拉取：把上次拿到的最大 `seq` 传给 `after_seq`，只取新增的那部分。
    """
    offset = runtime.settings.quota_timezone_offset_hours
    min_level = resolve_level(level)
    if level and min_level is None:
        raise invalid_input(f"无法识别的日志等级: {level}")

    items = LOG_BUFFER.snapshot(
        limit=limit,
        min_level=min_level,
        logger_prefix=logger_name,
        task_id=task_id,
        after_seq=after_seq,
    )
    return {
        "items": [
            {
                **{
                    key: value
                    for key, value in item.items()
                    if key not in ("created", "level_no")
                },
                "time": to_iso(
                    datetime.fromtimestamp(item["created"], UTC).replace(tzinfo=None),
                    offset,
                ),
            }
            for item in items
        ],
        "counts": LOG_BUFFER.level_counts(),
        "buffer": LOG_BUFFER.stats(),
        "filters": {
            "limit": limit,
            "level": level,
            "logger_name": logger_name,
            "task_id": task_id,
            "after_seq": after_seq,
        },
    }


@router.get("/metrics")
async def metrics(
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> dict[str, object]:
    settings = runtime.settings
    stats = runtime.pool.stats()

    queue_len = int(
        (
            await db.execute(
                select(func.count()).select_from(Task).where(Task.status == "queued")
            )
        ).scalar_one()
    )
    active = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.status.in_(("running", "streaming")))
            )
        ).scalar_one()
    )
    total = int(
        (await db.execute(select(func.count()).select_from(Task))).scalar_one()
    )
    failed = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.status.in_(("failed", "timeout")))
            )
        ).scalar_one()
    )

    return {
        "mode": "mock" if settings.mock_mode else "real",
        "engine": runtime.engine_state(),
        "pool": stats,
        "queue_len": queue_len,
        "active_tasks": active,
        "total_tasks": total,
        "failed_tasks": failed,
    }


@router.post("/users", response_model=AdminCreateUserResponse, status_code=201)
async def create_user(
    payload: AdminCreateUserRequest,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> AdminCreateUserResponse:
    existing = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise invalid_input("用户名已存在")
    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        password_hash=hash_password(payload.password),
        daily_quota=(
            payload.daily_quota
            if payload.daily_quota is not None
            else runtime.settings.default_daily_quota
        ),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return AdminCreateUserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        daily_quota=user.daily_quota,
    )


@router.get("/users")
async def list_users(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, object]:
    rows = (
        (await db.execute(select(User).order_by(User.id).limit(limit))).scalars().all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "display_name": row.display_name,
                "daily_quota": row.daily_quota,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.get("/users/{user_id}")
async def user_detail(
    user_id: int,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> dict[str, object]:
    """单个用户的详细信息：配额用量、任务分布、登录设备、实例粘性绑定。

    用户来问「我今天怎么用不了了」时看这个：先看 `quota.remaining_today`，
    再看 `tasks.by_status` 有没有堆在 queued，最后看 `devices` 有没有被吊销。
    """
    settings = runtime.settings
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")

    offset = settings.quota_timezone_offset_hours
    start, end = day_window(offset)
    used_stmt = (
        select(func.count())
        .select_from(Task)
        .where(Task.user_id == user_id, Task.created_at >= start, Task.created_at < end)
    )
    used_today = int((await db.execute(used_stmt)).scalar_one())

    statuses = ("queued", "running", "streaming", "succeeded", "failed", "timeout",
                "canceled")
    by_status = {
        status: await _count_tasks_in(db, (status,), user_id=user_id)
        for status in statuses
    }

    usage_stmt = select(
        func.coalesce(func.sum(Task.tokens_in), 0),
        func.coalesce(func.sum(Task.tokens_out), 0),
        func.coalesce(func.sum(Task.cost), 0.0),
    ).where(Task.user_id == user_id)
    tokens_in, tokens_out, cost = (await db.execute(usage_stmt)).one()

    recent = (
        (
            await db.execute(
                select(Task)
                .where(Task.user_id == user_id)
                .order_by(Task.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    devices = (
        (
            await db.execute(
                select(ApiClient)
                .where(ApiClient.user_id == user_id)
                .order_by(ApiClient.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    binding = await db.get(UserBinding, user_id)

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "external_user_id": user.external_user_id,
            "display_name": user.display_name,
            "enabled": user.enabled,
            "daily_quota": user.daily_quota,
            "created_at": to_iso(user.created_at, offset),
        },
        "quota": {
            "daily_quota": user.daily_quota,
            "used_today": used_today,
            "remaining_today": max(0, user.daily_quota - used_today),
            "day_start": to_iso(start, offset),
            "day_end": to_iso(end, offset),
        },
        "tasks": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "recent": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "status": row.status,
                    "instance_id": row.instance_id,
                    "chars_in": row.fetched_chars,
                    "tokens_out": row.tokens_out,
                    "error_code": row.error_code,
                    "created_at": to_iso(row.created_at, offset),
                    "finished_at": to_iso(row.finished_at, offset),
                }
                for row in recent
            ],
        },
        "usage": {
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "cost": round(float(cost), 6),
        },
        "devices": [
            {
                "id": row.id,
                "name": row.name,
                "scopes": row.scopes,
                "revoked": row.revoked_at is not None,
                "created_at": to_iso(row.created_at, offset),
                "last_used_at": to_iso(row.last_used_at, offset),
            }
            for row in devices
        ],
        "binding": (
            {
                "instance_id": binding.instance_id,
                "session_id": binding.session_id,
                "last_used_at": to_iso(binding.last_used_at, offset),
            }
            if binding is not None
            else None
        ),
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int, payload: AdminUpdateUserRequest, db: DbSession
) -> dict[str, object]:
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")
    if payload.daily_quota is not None:
        user.daily_quota = payload.daily_quota
    if payload.enabled is not None:
        user.enabled = payload.enabled
    if payload.password:
        user.password_hash = hash_password(payload.password)
    await db.commit()
    return {
        "id": user.id,
        "username": user.username,
        "daily_quota": user.daily_quota,
        "enabled": user.enabled,
    }
