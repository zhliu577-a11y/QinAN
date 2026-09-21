"""进程级运行时单例：事件总线、实例池、调度器、事件转发。

注意：这些组件都是进程内状态，因此网关必须以单 worker 运行
（uvicorn 不加 --workers，也不要开多副本）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from .core.config import Settings, get_settings
from .core.db import dispose_db, get_sessionmaker, init_db
from .services.dispatcher import Dispatcher
from .services.event_relay import EventRelay
from .services.events_bus import TaskEventBus
from .services.instance_pool import InstancePool

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = TaskEventBus()
        self.pool = InstancePool(settings)
        self.dispatcher = Dispatcher(self.pool, self.bus, settings)
        self.relay = EventRelay(self.pool, self.dispatcher)
        self.started = False

    async def start(self) -> None:
        await init_db()
        await self.pool.start()
        await self.dispatcher.start()
        await self.relay.start()
        self.started = True
        mode = "MOCK" if self.settings.mock_mode else "REAL"
        logger.info("网关运行时已启动（%s 模式）", mode)

    async def stop(self) -> None:
        self.started = False
        await self.relay.stop()
        await self.dispatcher.stop()
        await self.pool.stop()
        await dispose_db()

    def engine_state(self) -> str:
        stats = self.pool.stats()
        if stats["total"] == 0:
            return "down"
        if stats["healthy"] == 0:
            return "down"
        if stats["idle"] == 0:
            return "degraded"
        return "ready"

    @asynccontextmanager
    async def db_session(self) -> AsyncIterator[AsyncSession]:
        async with get_sessionmaker()() as session:
            yield session


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(get_settings())
    return _runtime


async def reset_runtime() -> None:
    """供测试使用：彻底释放运行时状态。"""
    global _runtime
    if _runtime is not None:
        await _runtime.stop()
    _runtime = None
