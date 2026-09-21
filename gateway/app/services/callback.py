"""任务终态回调：HMAC 签名 + 指数退避重试。"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from ..core.config import Settings
from ..core.db import get_sessionmaker
from ..core.security import sign_hmac_sha256_header
from ..core.timeutil import to_iso
from ..models import Task

logger = logging.getLogger(__name__)


def callback_url_allowed(url: str, allowed_hosts: set[str]) -> bool:
    """只允许白名单内的 HTTPS 地址，避免被当成 SSRF 跳板。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if not allowed_hosts:
        return False
    return host in allowed_hosts


class CallbackDelivery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, task_id: str) -> None:
        task = asyncio.create_task(self._deliver(task_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def _deliver(self, task_id: str) -> None:
        settings = self.settings
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is None or not task.callback_url:
                return
            url = task.callback_url
            body = {
                "task_id": task.id,
                "client_task_id": task.client_task_id,
                "status": task.status,
                "result_md": task.result_md,
                "error": (
                    {"code": task.error_code, "message": task.error_message}
                    if task.error_code
                    else None
                ),
                "finished_at": to_iso(
                    task.finished_at, settings.quota_timezone_offset_hours
                ),
            }
            task.callback_state = "pending"
            await db.commit()

        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Signature": sign_hmac_sha256_header(
                settings.callback_hmac_secret, raw
            ),
        }

        delays = list(settings.callback_retry_delays) + [0] * settings.callback_max_attempts
        for attempt in range(max(settings.callback_max_attempts, 1)):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.callback_timeout_seconds
                ) as client:
                    response = await client.post(url, content=raw, headers=headers)
                if 200 <= response.status_code < 300:
                    await self._set_state(task_id, "delivered")
                    return
                logger.warning(
                    "回调返回非 2xx task=%s status=%s", task_id, response.status_code
                )
            except Exception as exc:
                logger.warning("回调失败 task=%s attempt=%d: %s", task_id, attempt + 1, exc)

            delay = delays[attempt] if attempt < len(delays) else 0
            if delay:
                await asyncio.sleep(delay)

        await self._set_state(task_id, "failed")
        logger.error("回调重试耗尽 task=%s url=%s", task_id, url)

    async def _set_state(self, task_id: str, state: str) -> None:
        async with get_sessionmaker()() as db:
            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if task is not None:
                task.callback_state = state
                await db.commit()
