"""进程内日志环形缓冲，供 `/internal/logs` 读取。

为什么不用 `docker compose logs`：那需要宿主机权限，而给网关容器挂 docker
socket 等于把宿主机 root 交出去（挂上 socket 就能起特权容器），不值得。
所以这里只保留网关**自己**最近若干条日志 —— 足以在不开 SSH 的情况下回答
「网关刚才为什么拒绝了那个任务」。

只保留最近 N 条、进程重启即清空，这是刻意的：日志的权威副本仍然是 stdout
交给 docker 收集，这里只是个自检窗口。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

DEFAULT_CAPACITY = 2000

# 这些字段由业务代码通过 logger.info(..., extra={...}) 附加，有就带上
_EXTRA_FIELDS = ("task_id", "instance_id", "user_id", "session_id", "request_id")


class RingBufferHandler(logging.Handler):
    """把日志记录存进定长 deque，并允许按等级 / logger / task_id 过滤读取。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        # 日志处理器绝不能把异常抛回业务代码，也绝不能自己再写日志（会递归）
        try:
            with self._lock:
                self._seq += 1
                item: dict[str, Any] = {
                    "seq": self._seq,
                    "created": record.created,
                    "level": record.levelname,
                    "level_no": record.levelno,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                for field in _EXTRA_FIELDS:
                    value = getattr(record, field, None)
                    if value is not None:
                        item[field] = value
                if record.exc_info:
                    item["exception"] = self.formatException(record.exc_info)
                self._records.append(item)
        except Exception:  # pragma: no cover - 兜底
            self._dropped += 1

    def snapshot(
        self,
        *,
        limit: int = 200,
        min_level: int | None = None,
        logger_prefix: str | None = None,
        task_id: str | None = None,
        after_seq: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """按条件过滤后返回，最多 limit 条。默认返回**最新的** limit 条。"""
        with self._lock:
            items = list(self._records)

        def keep(item: dict[str, Any]) -> bool:
            if min_level is not None and item["level_no"] < min_level:
                return False
            if logger_prefix and not str(item["logger"]).startswith(logger_prefix):
                return False
            if after_seq is not None and item["seq"] <= after_seq:
                return False
            if task_id is not None and item.get("task_id") != task_id:
                return False
            return True

        filtered = [item for item in items if keep(item)]
        if newest_first:
            filtered = list(reversed(filtered))
        else:
            filtered = filtered[-limit:]
        return filtered[:limit]

    def level_counts(self) -> dict[str, int]:
        with self._lock:
            items = list(self._records)
        counts: dict[str, int] = {}
        for item in items:
            level = str(item["level"])
            counts[level] = counts.get(level, 0) + 1
        return counts

    def stats(self) -> dict[str, int]:
        with self._lock:
            size = len(self._records)
        return {"size": size, "capacity": self.capacity, "dropped": self._dropped}

    def clear(self) -> int:
        with self._lock:
            size = len(self._records)
            self._records.clear()
        return size


LOG_BUFFER = RingBufferHandler()


def install_log_buffer(level: int = logging.INFO) -> RingBufferHandler:
    """挂到 root logger 上。重复调用只生效一次。"""
    LOG_BUFFER.setLevel(level)
    root = logging.getLogger()
    if LOG_BUFFER not in root.handlers:
        root.addHandler(LOG_BUFFER)
    return LOG_BUFFER


def resolve_level(name: str | None) -> int | None:
    """把 "error" / "WARNING" 这类字符串转成 logging 等级数字；无法识别则 None。"""
    if not name:
        return None
    value = getattr(logging, str(name).upper(), None)
    return value if isinstance(value, int) else None
