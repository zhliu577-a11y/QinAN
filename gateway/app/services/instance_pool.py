"""实例池：实例注册表、健康检查、会话路由与空闲回收。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..core.config import Settings
from ..core.timeutil import utcnow
from .opencode_client import MockOpencodeClient, OpencodeClient

logger = logging.getLogger(__name__)

IDLE = "idle"
BUSY = "busy"
UNHEALTHY = "unhealthy"
STOPPED = "stopped"


@dataclass
class InstanceRuntime:
    id: str
    client: object
    status: str = IDLE
    current_task_id: str | None = None
    failures: int = 0
    busy_since: datetime | None = None
    idle_since: datetime = field(default_factory=utcnow)
    # session_id -> task_id，事件流据此把上游事件路由回具体任务
    sessions: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status == IDLE


class InstancePool:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._instances: list[InstanceRuntime] = []
        self._dropped_tasks: set[str] = set()
        self._health_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self.settings.mock_mode:
            client = MockOpencodeClient(
                "mock-1", step_delay=self.settings.mock_step_delay_seconds
            )
            await client.start()
            self._instances = [InstanceRuntime(id="mock-1", client=client)]
            logger.info("实例池已启动（MOCK 模式）：1 个模拟实例")
        else:
            runtimes: list[InstanceRuntime] = []
            for spec in self.settings.instance_specs:
                client = OpencodeClient(
                    spec,
                    self.settings.opencode_server_username,
                    self.settings.opencode_request_timeout_seconds,
                )
                await client.start()
                runtimes.append(InstanceRuntime(id=spec.id, client=client))
            if not runtimes:
                raise RuntimeError(
                    "非 MOCK 模式必须配置 OPENCODE_INSTANCES（host:port:password）"
                )
            self._instances = runtimes
            logger.info("实例池已启动：%d 个实例", len(runtimes))

        self._health_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        self._closed = True
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        for instance in self._instances:
            try:
                await instance.client.close()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("关闭实例客户端失败 %s: %s", instance.id, exc)
        self._instances = []

    # ---------- 查询 ----------

    @property
    def instances(self) -> list[InstanceRuntime]:
        return list(self._instances)

    def get(self, instance_id: str) -> InstanceRuntime | None:
        for instance in self._instances:
            if instance.id == instance_id:
                return instance
        return None

    def idle_instances(self) -> list[InstanceRuntime]:
        return [instance for instance in self._instances if instance.available]

    def task_of_session(self, session_id: str) -> tuple[InstanceRuntime, str] | None:
        for instance in self._instances:
            task_id = instance.sessions.get(session_id)
            if task_id:
                return instance, task_id
        return None

    def stats(self) -> dict[str, int]:
        healthy = [i for i in self._instances if i.status != UNHEALTHY]
        return {
            "total": len(self._instances),
            "idle": len(self.idle_instances()),
            "busy": len([i for i in self._instances if i.status == BUSY]),
            "unhealthy": len([i for i in self._instances if i.status == UNHEALTHY]),
            "healthy": len(healthy),
        }

    def snapshot(self) -> list[dict[str, object]]:
        """逐实例明细，供 /internal/instances 排查「哪个进程卡住了」。

        stats() 只给聚合数，看不出是哪个实例在忙、忙了多久、失败几次。
        """
        now = utcnow()
        rows: list[dict[str, object]] = []
        for instance in self._instances:
            busy_since = instance.busy_since
            idle_since = instance.idle_since
            rows.append(
                {
                    "id": instance.id,
                    "status": instance.status,
                    "base_url": getattr(instance.client, "base_url", ""),
                    "current_task_id": instance.current_task_id,
                    "failures": instance.failures,
                    "busy_seconds": (
                        int((now - busy_since).total_seconds())
                        if instance.status == BUSY and busy_since is not None
                        else 0
                    ),
                    "idle_seconds": (
                        int((now - idle_since).total_seconds())
                        if instance.status == IDLE and idle_since is not None
                        else 0
                    ),
                    "sessions": len(instance.sessions),
                }
            )
        return rows

    def mark_busy(self, instance: InstanceRuntime, task_id: str) -> None:
        instance.status = BUSY
        instance.current_task_id = task_id
        instance.busy_since = utcnow()

    def release(self, instance: InstanceRuntime) -> None:
        if instance.status == UNHEALTHY:
            return
        instance.status = IDLE
        instance.current_task_id = None
        instance.busy_since = None
        instance.idle_since = utcnow()

    def drop_task(self, task_id: str) -> None:
        self._dropped_tasks.add(task_id)

    def drain_dropped(self) -> set[str]:
        dropped = set(self._dropped_tasks)
        self._dropped_tasks.clear()
        return dropped

    # ---------- 健康检查 ----------

    async def _health_loop(self) -> None:
        interval = max(self.settings.pool_health_interval_seconds, 1)
        while not self._closed:
            try:
                await asyncio.sleep(interval)
                await self._check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 健康检查自身不能把循环打死
                logger.exception("健康检查异常: %s", exc)

    async def _check_all(self) -> None:
        for instance in list(self._instances):
            try:
                ok = await instance.client.health()  # type: ignore[attr-defined]
            except Exception:
                ok = False

            if ok:
                instance.failures = 0
                if instance.status == UNHEALTHY:
                    logger.info("实例 %s 已恢复", instance.id)
                    instance.status = IDLE
                    instance.idle_since = utcnow()
                continue

            instance.failures += 1
            if instance.failures >= max(self.settings.pool_health_failure_threshold, 1):
                self._mark_unhealthy(instance)

        await self._dispose_idle_instances()

    def _mark_unhealthy(self, instance: InstanceRuntime) -> None:
        if instance.status == UNHEALTHY:
            return
        logger.error(
            "实例 %s 连续 %d 次健康检查失败，标记为不可用", instance.id, instance.failures
        )
        if instance.current_task_id:
            self.drop_task(instance.current_task_id)
        instance.status = UNHEALTHY
        instance.current_task_id = None
        instance.busy_since = None
        # 会话映射一并清空：实例重启后 sessionID 不再有效
        instance.sessions.clear()

    async def _dispose_idle_instances(self) -> None:
        minutes = self.settings.instance_idle_dispose_minutes
        if minutes <= 0:
            return
        now = utcnow()
        for instance in self._instances:
            if instance.status != IDLE or instance.sessions:
                continue
            if (now - instance.idle_since).total_seconds() < minutes * 60:
                continue
            logger.info("实例 %s 空闲超时，释放项目实例", instance.id)
            try:
                await instance.client.dispose()  # type: ignore[attr-defined]
            finally:
                instance.idle_since = now
