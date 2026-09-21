"""/internal/logs 的环形缓冲，以及网关重启后的孤儿任务回收。"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
import pytest_asyncio
from app.core.logbuffer import LOG_BUFFER, RingBufferHandler
from app.models import Task
from sqlalchemy import select

from .conftest import login

ADMIN = {"X-Admin-Token": "test-admin-token"}


# ---------- 环形缓冲本体 ----------


def test_ring_buffer_keeps_only_capacity():
    handler = RingBufferHandler(capacity=3)
    logger = logging.getLogger("ringbuf.capacity.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        for i in range(5):
            logger.info("第 %d 条", i)
    finally:
        logger.removeHandler(handler)

    items = handler.snapshot(limit=10)
    assert [item["message"] for item in items] == ["第 2 条", "第 3 条", "第 4 条"]
    assert handler.stats() == {"size": 3, "capacity": 3, "dropped": 0}


def test_ring_buffer_filters_by_level_logger_and_task():
    handler = RingBufferHandler()
    logger = logging.getLogger("ringbuf.filter.test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info("普通", extra={"task_id": "tsk_a"})
        logger.warning("警告", extra={"task_id": "tsk_b"})
        logger.error("错误", extra={"task_id": "tsk_a"})
    finally:
        logger.removeHandler(handler)

    assert len(handler.snapshot()) == 3
    assert [i["message"] for i in handler.snapshot(min_level=logging.WARNING)] == [
        "警告",
        "错误",
    ]
    assert [i["message"] for i in handler.snapshot(task_id="tsk_a")] == ["普通", "错误"]
    assert handler.snapshot(logger_prefix="ringbuf.filter") != []
    assert handler.snapshot(logger_prefix="根本没有这个 logger") == []
    assert handler.level_counts() == {"INFO": 1, "WARNING": 1, "ERROR": 1}


def test_ring_buffer_after_seq_returns_only_new():
    handler = RingBufferHandler()
    logger = logging.getLogger("ringbuf.seq.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        for i in range(4):
            logger.info("第 %d 条", i)
    finally:
        logger.removeHandler(handler)

    # snapshot 默认给「最新的 N 条」，所以 limit=2 拿到的是最后两条（seq 3、4），
    # 而不是前两条 —— 增量拉取要基于这个语义来对 seq。
    assert [i["message"] for i in handler.snapshot(limit=2)] == ["第 2 条", "第 3 条"]

    second_seq = handler.snapshot()[1]["seq"]
    newer = handler.snapshot(after_seq=second_seq)
    assert [i["message"] for i in newer] == ["第 2 条", "第 3 条"]
    assert [i["seq"] for i in newer] == [3, 4]


def test_ring_buffer_never_raises_on_broken_record():
    """日志处理器抛异常会把业务代码带崩，所以它必须自己吞掉。"""

    class Exploding(RingBufferHandler):
        def formatException(self, ei):  # type: ignore[override]
            raise RuntimeError("格式化炸了")

    handler = Exploding()
    record = logging.LogRecord(
        "x", logging.ERROR, "f", 1, "msg", None, (ValueError, ValueError("boom"), None)
    )
    handler.emit(record)
    assert handler.stats()["dropped"] == 1


# ---------- /internal/logs ----------


async def test_logs_requires_admin(client):
    assert (await client.get("/internal/logs")).status_code == 403


async def test_logs_endpoint_returns_and_filters(client):
    LOG_BUFFER.clear()
    logger = logging.getLogger("app.services.test_marker")
    logger.error("端点上应该看得到这条", extra={"task_id": "tsk_logs_test"})

    response = await client.get("/internal/logs", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["buffer"]["capacity"] == LOG_BUFFER.capacity
    assert body["items"], "至少应该有刚才那条"

    item = next(i for i in body["items"] if i.get("task_id") == "tsk_logs_test")
    assert item["level"] == "ERROR"
    assert item["message"] == "端点上应该看得到这条"
    assert item["logger"] == "app.services.test_marker"
    assert "time" in item
    assert "created" not in item and "level_no" not in item

    filtered = (
        await client.get(
            "/internal/logs", headers=ADMIN, params={"task_id": "tsk_logs_test"}
        )
    ).json()
    assert all(i.get("task_id") == "tsk_logs_test" for i in filtered["items"])

    only_error = (
        await client.get("/internal/logs", headers=ADMIN, params={"level": "error"})
    ).json()
    assert all(i["level"] == "ERROR" for i in only_error["items"])


async def test_logs_rejects_unknown_level(client):
    response = await client.get(
        "/internal/logs", headers=ADMIN, params={"level": "灾难"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_INPUT"


async def test_logs_limit_is_bounded(client):
    # 参数校验失败会被统一错误处理器翻成 400 INVALID_INPUT（见 core/errors.py），
    # 不是 FastAPI 默认的 422 —— 这里跟着项目的既有约定走。
    for bad in (0, 100000):
        response = await client.get(
            "/internal/logs", headers=ADMIN, params={"limit": bad}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_INPUT"


# ---------- 重启后的孤儿任务回收 ----------


async def _make_task(user_id: int, task_id: str, status: str, *, retried: bool = False):
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="text",
                input_text="正文",
                status=status,
                retried=retried,
                instance_id="oc-1",
                session_id="ses_old",
            )
        )
        await db.commit()


async def _read_task(task_id: str):
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        return await db.get(Task, task_id)


@pytest_asyncio.fixture
async def orphan_env(runtime):
    """造孤儿任务的环境：暂停调度循环，并在结束后把造出来的行清干净。

    两件事都必须做，否则会污染别的测试（实测踩过）：

    1. **必须暂停调度**。_recover_orphans 会把任务改回 queued，而后台调度器
       每 0.05s 跑一次，会立刻把它派到唯一那个 mock 实例上。于是一串孤儿任务
       在单实例上串行排队，把后面所有测试堵在 queued，20s 都不进终态。
    2. **必须删掉造出来的行**。恢复调度后它们还在库里，照样会被派发。

    不这么做就等于用共享的单实例池做全局副作用，测试之间互相拖。
    """
    dispatcher = runtime.dispatcher
    dispatcher._closed = True
    if dispatcher._loop_task is not None:
        dispatcher._loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher._loop_task
        dispatcher._loop_task = None

    created: list[str] = []

    async def make(
        task_id: str, user_id: int, status: str, *, retried: bool = False
    ) -> str:
        await _make_task(user_id, task_id, status, retried=retried)
        created.append(task_id)
        return task_id

    try:
        yield dispatcher, make
    finally:
        if created:
            from app.core.db import get_sessionmaker
            from app.models import TaskEvent
            from sqlalchemy import delete

            async with get_sessionmaker()() as db:
                for task_id in created:
                    await db.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
                    row = await db.get(Task, task_id)
                    if row is not None:
                        await db.delete(row)
                await db.commit()
        dispatcher._closed = False
        dispatcher._loop_task = asyncio.create_task(dispatcher._run())


async def _login_and_id(client, new_user) -> tuple[str, int]:
    username, password = await new_user()
    token = await login(client, username, password)
    me = (
        await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    ).json()
    return username, me["id"]


async def test_recover_orphans_requeues_interrupted_task(client, new_user, orphan_env):
    """进程重启后在 running 的任务没人推进，必须被放回队列，否则客户端永远等不到终态。"""
    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    task_id = await make("tsk_orphan_requeue", user_id, "running")
    await dispatcher._recover_orphans()

    task = await _read_task(task_id)
    assert task.status == "queued"
    assert task.retried is True
    assert task.instance_id is None
    assert task.session_id is None
    assert task.started_at is None


async def test_recover_orphans_fails_task_that_already_retried(client, new_user, orphan_env):
    """已经重试过一次的不能再回队列，否则会无限重试。"""
    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    task_id = await make("tsk_orphan_retried", user_id, "streaming", retried=True)
    await dispatcher._recover_orphans()

    task = await _read_task(task_id)
    assert task.status == "failed"
    assert task.error_code == "GATEWAY_RESTARTED"
    assert task.finished_at is not None


async def test_recover_orphans_leaves_other_statuses_alone(client, new_user, orphan_env):
    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    await make("tsk_orphan_queued", user_id, "queued")
    await make("tsk_orphan_done", user_id, "succeeded")
    await dispatcher._recover_orphans()

    assert (await _read_task("tsk_orphan_queued")).retried is False
    assert (await _read_task("tsk_orphan_done")).status == "succeeded"


async def test_recover_orphans_is_idempotent(client, new_user, orphan_env):
    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    task_id = await make("tsk_orphan_twice", user_id, "running")
    await dispatcher._recover_orphans()
    await dispatcher._recover_orphans()

    # 第二次不该再动它：已经是 queued 了
    task = await _read_task(task_id)
    assert task.status == "queued"
    assert task.retried is True


async def test_recovered_task_emits_event_for_reconnect(
    client, new_user, orphan_env, session_factory
):
    """回收时要落一条事件，断线重连的客户端才知道状态变了。"""
    from app.models import TaskEvent

    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    task_id = await make("tsk_orphan_event", user_id, "running")
    await dispatcher._recover_orphans()

    async with session_factory() as db:
        rows = (
            (await db.execute(select(TaskEvent).where(TaskEvent.task_id == task_id)))
            .scalars()
            .all()
        )
    assert any('"queued"' in row.payload for row in rows)


@pytest.mark.parametrize("status", ["running", "streaming"])
async def test_recover_orphans_covers_both_active_statuses(
    client, new_user, orphan_env, status
):
    dispatcher, make = orphan_env
    _, user_id = await _login_and_id(client, new_user)

    task_id = await make(f"tsk_orphan_status_{status}", user_id, status)
    await dispatcher._recover_orphans()
    assert (await _read_task(task_id)).status == "queued"
