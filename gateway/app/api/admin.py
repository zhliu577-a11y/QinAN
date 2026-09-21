"""管理接口，只在内网可达（nginx 已限制 /internal/ 的来源网段）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from ..core.errors import invalid_input, not_found
from ..core.security import hash_password
from ..models import Task, User
from ..runtime import Runtime
from ..schemas import (
    AdminCreateUserRequest,
    AdminCreateUserResponse,
    AdminUpdateUserRequest,
)
from .deps import DbSession, require_admin_token, runtime_dep

router = APIRouter(
    prefix="/internal",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


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
