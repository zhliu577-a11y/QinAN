"""取消与派发/完成之间的竞态。

回归的 bug：提交后立刻取消，接口返回 200 {"status":"canceled"}，但任务最终是
succeeded 并产出了结果——取消等于没生效，token 照烧。

根因是状态推进用了「先读后写」。调度器把 status=queued 读进内存后，取消事务先提交
canceled，随后 _start 的无条件写入把终态覆盖回 running；再后来完成路径又把 running
推进到 succeeded。修法是把所有状态推进改成条件更新（同一条 UPDATE 里判定并写入），
并让 cancel 在赢下终态后补发一次 abort。
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


async def test_finalize_does_not_overwrite_terminal_state(runtime, session_factory, new_user):
    """终态只能被写一次：完成路径不得覆盖已落定的 canceled。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = "tsk_" + secrets.token_hex(12)

    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="text",
                input_text="终态竞态",
                status="canceled",
            )
        )
        await db.commit()

    # 模拟完成路径带着结果来收尾
    won = await runtime.dispatcher._finalize(task_id, "succeeded", result_md="不该被写入")
    assert won is False, "已有终态时 _finalize 应返回 False"

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "canceled"
        assert row.result_md is None


async def test_cancel_wins_over_concurrent_dispatch(runtime, session_factory, new_user):
    """取消先落定后，派发必须放弃；反之取消要回报真实状态。"""
    username, _ = await new_user()
    user_id = await _user_id(session_factory, username)
    task_id = "tsk_" + secrets.token_hex(12)

    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                user_id=user_id,
                kind="text",
                input_text="取消优先",
                status="queued",
            )
        )
        await db.commit()

    assert await runtime.dispatcher.cancel(task_id) == "canceled"

    # 取消已落定，此时即使派发路径拿着排队快照进来，也不该把任务抢走
    stale = Task(
        id=task_id,
        user_id=user_id,
        kind="text",
        input_text="取消优先",
        status="queued",
    )
    instance = runtime.pool.idle_instances()[0]
    await runtime.dispatcher._start(stale, instance, None)

    async with session_factory() as db:
        row = await db.get(Task, task_id)
        assert row.status == "canceled"
        assert row.instance_id is None
        assert row.session_id is None

    # 终态任务再取消一次，应回报真实状态而不是谎报 canceled
    assert await runtime.dispatcher.cancel(task_id) == "canceled"


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
