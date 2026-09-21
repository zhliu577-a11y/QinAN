"""webfetch 抓取元信息（fetched_chars / source_title）的解析与上报。"""

from __future__ import annotations

import secrets

import pytest
from app.models import Task, User
from app.services.dispatcher import Dispatcher
from app.services.event_relay import EventRelay, _clean_fetch_title
from sqlalchemy import select


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


async def test_reused_call_id_across_sessions_is_still_reported():
    """回归：callID 由模型生成，只在单次响应内唯一。

    mock 固定回 call_mock_1、真实模型也常见 call_0，所以第二个任务起
    会带着同一个 callID 再来一次。曾经的全局去重集合把它当成重复，
    于是从第二个 url 任务开始 fetched_chars 全是 None。
    """
    relay, dispatcher = _relay()
    await relay._handle_tool("ses_1", _part("completed", output="正文" * 10))
    await relay._handle_tool("ses_2", _part("completed", output="正文" * 10))

    assert [(session, chars) for session, chars, _ in dispatcher.calls] == [
        ("ses_1", 20),
        ("ses_2", 20),
    ]


async def test_second_fetch_in_same_session_is_counted():
    """同一会话里模型抓第二次网页，partID 不同就该再记一次。"""
    relay, dispatcher = _relay()
    await relay._handle_tool("ses_1", _part("completed", output="a" * 10))
    second = {**_part("completed", output="b" * 20), "id": "prt_2"}
    await relay._handle_tool("ses_1", second)

    assert [chars for _, chars, _ in dispatcher.calls] == [10, 20]


async def test_forget_session_releases_dedup_entries():
    relay, dispatcher = _relay()
    await relay._handle_tool("ses_1", _part("completed", output="正文" * 10))
    assert relay._seen_fetch_total == 1

    relay._forget_session("ses_1")

    assert relay._seen_fetch_total == 0
    assert relay._seen_fetch_parts == {}


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
    assert dispatcher._fetch["tsk_1"] == {
        "chars": 150,
        "title": "https://a.com/",
        "calls": 2,
    }

    # 会话不在池里（任务已终态或不属于本进程）时不应写入
    await dispatcher.handle_fetch("ses_missing", 10, None)
    assert list(dispatcher._fetch) == ["tsk_1"]


async def _user_id(session_factory, username: str) -> int:
    async with session_factory() as db:
        return (await db.execute(select(User).where(User.username == username))).scalar_one().id


async def _running_url_task(session_factory, user_id: int) -> str:
    task_id = "tsk_" + secrets.token_hex(12)
    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="url",
                input_url="https://example.com/article",
                status="running",
            )
        )
        await db.commit()
    return task_id


async def test_finalize_writes_fetched_chars_for_url_task(
    runtime, session_factory, new_user
):
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _running_url_task(session_factory, user_id)

    runtime.dispatcher._fetch[task_id] = {"chars": 8213, "title": "示例文章", "calls": 1}
    assert await runtime.dispatcher._finalize(task_id, "succeeded", result_md="摘要") is True

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.fetched_chars == 8213
        assert row.source_title == "示例文章"


async def test_finalize_marks_empty_fetch_as_zero(runtime, session_factory, new_user):
    """抓取成功但正文为空应写 0；一次都没抓成才留 None。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)

    empty = await _running_url_task(session_factory, user_id)
    runtime.dispatcher._fetch[empty] = {"chars": 0, "title": None, "calls": 1}
    await runtime.dispatcher._finalize(empty, "succeeded", result_md="摘要")

    never = await _running_url_task(session_factory, user_id)
    await runtime.dispatcher._finalize(never, "succeeded", result_md="摘要")

    async with session_factory() as db:
        assert (await db.get(Task, empty)).fetched_chars == 0
        assert (await db.get(Task, never)).fetched_chars is None
