"""排队期间取消，不能被派发路径覆盖回 running。

回归的 bug：调度器把 status=queued 的任务读进内存后，用户点了取消，库里已变成
canceled；但 _start 无条件写 running，把终态覆盖掉，任务照常跑完。用户看到的是
「取消接口返回 200 canceled，任务最终却是 succeeded」。
"""

from __future__ import annotations

import secrets

from sqlalchemy import select

from app.models import Task, User


async def _user_id(session_factory, username: str) -> int:
    async with session_factory() as db:
        return (await db.execute(select(User).where(User.username == username))).scalar_one().id


async def test_start_refuses_task_already_canceled(runtime, session_factory, new_user):
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = "tsk_" + secrets.token_hex(12)

    # 库里已经是终态
    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="text",
                input_text="竞态回归",
                status="canceled",
            )
        )
        await db.commit()

    # 内存里是排队时的旧快照，状态还停留在 queued
    stale = Task(
        id=task_id,
        user_id=user_id,
        kind="text",
        input_text="竞态回归",
        status="queued",
    )

    instance = runtime.pool.idle_instances()[0]
    sessions_before = set(instance.sessions)

    await runtime.dispatcher._start(stale, instance, None)

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "canceled", "派发不得把终态覆盖回 running"
        assert row.session_id is None
        assert row.instance_id is None
        assert row.started_at is None

    assert set(instance.sessions) == sessions_before, "放弃派发后不该留下会话登记"
    assert instance.available, "放弃派发后实例应回到空闲"


async def test_defer_does_not_resurrect_canceled_task(runtime, session_factory, new_user):
    """下发失败走 _defer 时，已取消的任务不能被拉回队列。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = "tsk_" + secrets.token_hex(12)

    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="text",
                input_text="取消后下发失败",
                status="canceled",
            )
        )
        await db.commit()

    await runtime.dispatcher._defer(task_id)

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "canceled"
        assert row.retried is False
