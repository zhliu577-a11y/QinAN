"""时间处理：数据库统一存 naive UTC，对外输出按配置时区偏移补上时区。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。SQLite 无时区支持，存 naive 可避免读写不一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


def unix_now() -> int:
    return int(datetime.now(UTC).timestamp())


def to_iso(value: datetime | None, offset_hours: int = 8) -> str | None:
    if value is None:
        return None
    tz = timezone(timedelta(hours=offset_hours))
    return value.replace(tzinfo=UTC).astimezone(tz).isoformat()


def day_window(offset_hours: int = 8) -> tuple[datetime, datetime]:
    """返回配额统计所用的「今天」在 UTC 下的起止边界（含左不含右）。"""
    tz = timezone(timedelta(hours=offset_hours))
    local_now = datetime.now(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    start = local_start.astimezone(UTC).replace(tzinfo=None)
    end = local_end.astimezone(UTC).replace(tzinfo=None)
    return start, end
