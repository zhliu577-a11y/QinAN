"""超时回收与完成回调。

这两条路径原先只有「参数校验」级别的覆盖（callback_url 白名单），
真正的回收动作与投递过程一次都没跑过。这里全部在进程内模拟：
模型侧用 MOCK_MODE，回调目标用假的 httpx 客户端，不依赖任何外部地址。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import types
from datetime import timedelta

import pytest
from app.core.timeutil import utcnow
from app.models import Task, User
from app.services import callback as callback_module
from app.services.callback import callback_url_allowed
from sqlalchemy import select


async def _user_id(session_factory, username: str) -> int:
    async with session_factory() as db:
        return (await db.execute(select(User).where(User.username == username))).scalar_one().id


async def _add_task(session_factory, user_id: int, **fields) -> str:
    task_id = "tsk_" + secrets.token_hex(12)
    async with session_factory() as db:
        db.add(Task(id=task_id, user_id=user_id, kind="text", input_text="样本", **fields))
        await db.commit()
    return task_id


# ---------- 超时回收 ----------


async def test_reap_timeouts_finalizes_and_aborts(runtime, session_factory, new_user):
    """跑太久的活动任务要被回收成 timeout，并且真的中止上游会话。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    instance = runtime.pool.idle_instances()[0]
    session_id = "ses_" + secrets.token_hex(6)

    timeout_seconds = runtime.settings.task_timeout_seconds
    task_id = await _add_task(
        session_factory,
        user_id,
        status="running",
        instance_id=instance.id,
        session_id=session_id,
        started_at=utcnow() - timedelta(seconds=timeout_seconds + 60),
    )

    aborted: list[str] = []

    async def fake_abort(target: str) -> None:
        aborted.append(target)

    original = instance.client.abort
    instance.client.abort = fake_abort  # type: ignore[method-assign]
    instance.sessions[session_id] = task_id
    runtime.pool.mark_busy(instance, task_id)
    try:
        await runtime.dispatcher._reap_timeouts()
    finally:
        instance.client.abort = original  # type: ignore[method-assign]

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "timeout"
        assert row.error_code == "TIMEOUT"
        assert row.finished_at is not None
        assert row.result_md is None

    assert aborted == [session_id], "超时必须中止上游会话，别让模型继续烧 token"
    assert instance.available, "回收后实例要回到池子里"
    assert session_id not in instance.sessions


async def test_reap_timeouts_leaves_fresh_tasks_alone(runtime, session_factory, new_user):
    """还没到时限的任务不能被误收。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(
        session_factory,
        user_id,
        status="running",
        started_at=utcnow(),
    )

    await runtime.dispatcher._reap_timeouts()

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "running"
        assert row.error_code is None


async def test_reap_timeouts_ignores_queued_tasks(runtime, session_factory, new_user):
    """排队中的任务没有 started_at，不该被超时逻辑碰到。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(session_factory, user_id, status="queued")

    await runtime.dispatcher._reap_timeouts()

    async with session_factory() as db:
        assert (await db.get(Task, task_id)).status == "queued"


# ---------- 回调投递 ----------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _install_fake_httpx(monkeypatch, status_codes: list[int]) -> list[dict]:
    """把回调模块里的 httpx 换成假的，记录每次投递的 url / 正文 / 头。"""
    calls: list[dict] = []
    remaining = list(status_codes)

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def post(self, url, content=None, headers=None) -> _FakeResponse:
            calls.append({"url": url, "content": content, "headers": headers})
            code = remaining.pop(0) if remaining else 500
            return _FakeResponse(code)

    monkeypatch.setattr(
        callback_module, "httpx", types.SimpleNamespace(AsyncClient=FakeAsyncClient)
    )
    return calls


@pytest.fixture
def fast_callback_retries(runtime, monkeypatch):
    """退避间隔在测试里必须接近 0，否则一次失败重试要等两分钟。"""
    monkeypatch.setattr(runtime.settings, "callback_retry_delays", [0, 0, 0])
    monkeypatch.setattr(runtime.settings, "callback_max_attempts", 3)
    return runtime.settings


async def test_callback_delivers_signed_payload(
    runtime, session_factory, new_user, fast_callback_retries, monkeypatch
):
    calls = _install_fake_httpx(monkeypatch, [200])
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(
        session_factory,
        user_id,
        status="succeeded",
        result_md="# 摘要\n\n- 要点",
        callback_url="https://app.example.com/hook",
        client_task_id="cli-1",
        finished_at=utcnow(),
    )

    await runtime.dispatcher.callbacks._deliver(task_id)

    assert len(calls) == 1, "成功后不该重试"
    sent = calls[0]
    assert sent["url"] == "https://app.example.com/hook"

    expected = hmac.new(
        runtime.settings.callback_hmac_secret.encode(), sent["content"], hashlib.sha256
    ).hexdigest()
    assert sent["headers"]["X-Agent-Signature"] == f"sha256={expected}"
    assert sent["headers"]["Content-Type"] == "application/json"

    body = json.loads(sent["content"].decode("utf-8"))
    assert body["task_id"] == task_id
    assert body["client_task_id"] == "cli-1"
    assert body["status"] == "succeeded"
    assert body["result_md"] == "# 摘要\n\n- 要点"
    assert body["error"] is None
    assert body["finished_at"]

    async with session_factory() as db:
        assert (await db.get(Task, task_id)).callback_state == "delivered"


async def test_callback_retries_then_marks_failed(
    runtime, session_factory, new_user, fast_callback_retries, monkeypatch
):
    """对端持续 5xx 时要重试到上限，最后如实记为 failed，不能假装成功。"""
    calls = _install_fake_httpx(monkeypatch, [500, 500, 500])
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(
        session_factory,
        user_id,
        status="succeeded",
        result_md="结果",
        callback_url="https://app.example.com/hook",
    )

    await runtime.dispatcher.callbacks._deliver(task_id)

    assert len(calls) == 3, "失败要按配置重试满 3 次"
    async with session_factory() as db:
        assert (await db.get(Task, task_id)).callback_state == "failed"


async def test_callback_recovers_on_second_attempt(
    runtime, session_factory, new_user, fast_callback_retries, monkeypatch
):
    """第一次 503、第二次 200，最终要记为 delivered。"""
    calls = _install_fake_httpx(monkeypatch, [503, 200])
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(
        session_factory,
        user_id,
        status="succeeded",
        result_md="结果",
        callback_url="https://app.example.com/hook",
    )

    await runtime.dispatcher.callbacks._deliver(task_id)

    assert len(calls) == 2
    async with session_factory() as db:
        assert (await db.get(Task, task_id)).callback_state == "delivered"


async def test_callback_skipped_without_url(
    runtime, session_factory, new_user, fast_callback_retries, monkeypatch
):
    calls = _install_fake_httpx(monkeypatch, [200])
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(session_factory, user_id, status="succeeded", result_md="结果")

    await runtime.dispatcher.callbacks._deliver(task_id)

    assert calls == []
    async with session_factory() as db:
        assert (await db.get(Task, task_id)).callback_state == "none"


async def test_finalize_schedules_callback(
    runtime, session_factory, new_user, fast_callback_retries, monkeypatch
):
    """终态收尾要真的把回调发出去，而不是只在库里留个状态。"""
    calls = _install_fake_httpx(monkeypatch, [200])
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = await _add_task(
        session_factory,
        user_id,
        status="running",
        callback_url="https://app.example.com/hook",
    )

    assert await runtime.dispatcher._finalize(task_id, "succeeded", result_md="完成") is True

    for _ in range(100):
        async with session_factory() as db:
            state = (await db.get(Task, task_id)).callback_state
        if state == "delivered":
            break
        await asyncio.sleep(0.02)

    assert state == "delivered", f"回调最终应投递成功，实际 {state}"
    assert len(calls) == 1
    assert json.loads(calls[0]["content"].decode())["status"] == "succeeded"


# ---------- 白名单 ----------


@pytest.mark.parametrize(
    ("url", "allowed", "expected"),
    [
        ("https://app.example.com/hook", {"app.example.com"}, True),
        ("https://APP.example.com/hook", {"app.example.com"}, True),
        ("http://app.example.com/hook", {"app.example.com"}, False),
        ("https://evil.example.net/hook", {"app.example.com"}, False),
        ("https://app.example.com/hook", set(), False),
        ("https://app.example.com.evil.net/hook", {"app.example.com"}, False),
        ("not-a-url", {"app.example.com"}, False),
    ],
)
def test_callback_url_allowlist(url, allowed, expected):
    assert callback_url_allowed(url, allowed) is expected
