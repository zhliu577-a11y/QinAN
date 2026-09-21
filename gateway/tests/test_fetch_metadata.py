"""webfetch 抓取元信息（fetched_chars / source_title）的解析与上报。"""

from __future__ import annotations

import pytest
from app.services.dispatcher import Dispatcher
from app.services.event_relay import EventRelay, _clean_fetch_title


class _StubDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def handle_fetch(self, session_id: str, chars: int, title: str | None) -> None:
        self.calls.append((session_id, chars, title))


class _StubPool:
    def task_of_session(self, session_id: str):
        if session_id == "ses_1":
            return ("inst-1", "tsk_1")
        return None


def _relay() -> tuple[EventRelay, _StubDispatcher]:
    dispatcher = _StubDispatcher()
    relay = EventRelay(None, dispatcher)  # type: ignore[arg-type]
    return relay, dispatcher


def _part(
    status: str,
    *,
    tool: str = "webfetch",
    output: str = "",
    title: str = "",
    call_id: str = "call_1",
) -> dict:
    return {
        "type": "tool",
        "id": "prt_1",
        "callID": call_id,
        "sessionID": "ses_1",
        "tool": tool,
        "state": {"status": status, "output": output, "title": title},
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/ (text/html;charset=UTF-8)", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("示例文章", "示例文章"),
        ("Fetch failed", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_fetch_title(raw, expected):
    assert _clean_fetch_title(raw) == expected


async def test_completed_webfetch_is_reported_once():
    relay, dispatcher = _relay()
    part = _part(
        "completed",
        output="正文" * 10,
        title="https://example.com/ (text/html;charset=UTF-8)",
    )

    await relay._handle_tool("ses_1", part)
    assert dispatcher.calls == [("ses_1", 20, "https://example.com/")]

    # 同一个 callID 重复推送（如 output 后续补全）不应重复计数
    await relay._handle_tool("ses_1", part)
    assert len(dispatcher.calls) == 1


async def test_unfinished_or_other_tool_is_ignored():
    relay, dispatcher = _relay()

    for status in ("pending", "running"):
        await relay._handle_tool("ses_1", _part(status, output="x" * 5))
    await relay._handle_tool("ses_1", _part("completed", tool="bash", output="x" * 5))

    assert dispatcher.calls == []


async def test_handle_fetch_accumulates_per_task():
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher._fetch = {}
    dispatcher.pool = _StubPool()  # type: ignore[assignment]

    await dispatcher.handle_fetch("ses_1", 100, "https://a.com/")
    await dispatcher.handle_fetch("ses_1", 50, "https://b.com/")
    assert dispatcher._fetch["tsk_1"] == {"chars": 150, "title": "https://a.com/"}

    # 会话不在池里（任务已终态或不属于本进程）时不应写入
    await dispatcher.handle_fetch("ses_missing", 10, None)
    assert list(dispatcher._fetch) == ["tsk_1"]
