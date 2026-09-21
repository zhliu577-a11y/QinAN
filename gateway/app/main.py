"""FastAPI 应用装配。

必须单 worker 运行：实例池、调度器与事件转发都是进程内状态。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from .api import admin, auth, streams, tasks
from .core.config import Settings, get_settings
from .core.db import ping_db
from .core.errors import AppError, app_error_handler, validation_error_handler
from .core.logbuffer import install_log_buffer
from .models import Task
from .runtime import get_runtime
from .schemas import HealthResponse

logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # stdout 交给 docker 收集（权威副本），环形缓冲是给 /internal/logs 的自检窗口。
    # 这里用 level 而不是 INFO：LOG_LEVEL 调成 DEBUG 时，自检也应该看得到 DEBUG。
    install_log_buffer(level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = get_runtime()
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings)

    app = FastAPI(
        title="智能体摘要服务网关",
        version="1.0.0",
        description=(
            "面向 App 的摘要任务 API。字段与错误码定义见 docs/API.md。"
        ),
        lifespan=lifespan,
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(streams.router, prefix="/api/v1")
    app.include_router(admin.router)

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        runtime = get_runtime()
        stats = runtime.pool.stats()
        queue_len = 0
        db_ok = await ping_db()
        if db_ok:
            async with runtime.db_session() as db:
                queue_len = int(
                    (
                        await db.execute(
                            select(func.count())
                            .select_from(Task)
                            .where(Task.status == "queued")
                        )
                    ).scalar_one()
                )
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            engine=runtime.engine_state(),
            pool_idle=stats["idle"],
            pool_total=stats["total"],
            queue_len=queue_len,
        )

    return app


app = create_app()
