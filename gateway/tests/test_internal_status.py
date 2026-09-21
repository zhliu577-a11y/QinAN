"""/internal 状态类接口：网关自检、opencode 进程明细、单用户详情。"""

from __future__ import annotations

import asyncio

from .conftest import login

ADMIN = {"X-Admin-Token": "test-admin-token"}


async def _run_one_task(client, token: str) -> str:
    response = await client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"kind": "text", "text": "待摘要的正文", "instruction": "总结"},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    for _ in range(200):
        detail = await client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if detail.json()["status"] in ("succeeded", "failed", "timeout", "canceled"):
            return task_id
        await asyncio.sleep(0.05)
    raise AssertionError("任务未在预期时间内结束")


async def test_requires_admin_token(client):
    for path in ("/internal/status", "/internal/instances", "/internal/users/1"):
        assert (await client.get(path)).status_code == 403
        assert (
            await client.get(path, headers={"X-Admin-Token": "wrong"})
        ).status_code == 403


async def test_status_reports_gateway_health(client):
    response = await client.get("/internal/status", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["gateway"]["ready"] is True
    assert body["gateway"]["uptime_seconds"] >= 0
    assert body["mode"] == "mock"
    assert body["engine"] == "ready"

    names = {check["name"] for check in body["checks"]}
    assert names == {"runtime_started", "database", "engine"}
    assert all(check["ok"] for check in body["checks"])
    assert body["status"] == "ok"

    assert body["pool"]["total"] >= 1
    assert isinstance(body["queue_len"], int)
    assert body["config"]["max_in_flight_per_user"] == 1


async def test_status_degrades_when_engine_has_no_idle_instance(client, runtime):
    """engine 不是 ready 时整体判 degraded，而不是等调用方自己解释 pool 数字。"""
    instance = runtime.pool.instances[0]
    original_status = instance.status
    try:
        runtime.pool.mark_busy(instance, "tsk_fake_for_status_test")
        body = (await client.get("/internal/status", headers=ADMIN)).json()
        assert body["engine"] == "degraded"
        assert body["status"] == "degraded"
        engine_check = next(c for c in body["checks"] if c["name"] == "engine")
        assert engine_check["ok"] is False
    finally:
        instance.status = original_status
        instance.current_task_id = None


async def test_instances_exposes_each_opencode_process(client):
    response = await client.get("/internal/instances", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["pool"]["total"] == len(body["items"])
    assert body["items"], "至少要有一个实例"
    for item in body["items"]:
        assert item["id"]
        assert item["status"] in ("idle", "busy", "unhealthy", "stopped")
        assert item["busy_seconds"] >= 0
        assert item["idle_seconds"] >= 0
        assert item["sessions"] >= 0
        assert "base_url" in item


async def test_instances_shows_the_busy_one_while_running(client, new_user):
    username, password = await new_user()
    token = await login(client, username, password)

    submit = await client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"kind": "text", "text": "正文", "instruction": "总结"},
    )
    assert submit.status_code == 201, submit.text

    seen_busy = False
    for _ in range(200):
        items = (await client.get("/internal/instances", headers=ADMIN)).json()["items"]
        busy = [i for i in items if i["status"] == "busy"]
        if busy:
            assert busy[0]["current_task_id"]
            seen_busy = True
            break
        await asyncio.sleep(0.02)
    assert seen_busy, "任务执行期间应当能看到 busy 的实例"

    task_id = submit.json()["task_id"]
    for _ in range(200):
        detail = await client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if detail.json()["status"] == "succeeded":
            break
        await asyncio.sleep(0.05)


async def test_user_detail_reports_quota_tasks_and_devices(client, new_user):
    username, password = await new_user(quota=5)
    token = await login(client, username, password)
    await _run_one_task(client, token)

    me = (await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    user_id = me["id"]

    response = await client.get(f"/internal/users/{user_id}", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["user"]["id"] == user_id
    assert body["user"]["username"] == username
    assert body["user"]["enabled"] is True
    # 密码哈希绝不外泄，哪怕调用方持管理员令牌
    assert "password_hash" not in body["user"]

    assert body["quota"]["daily_quota"] == 5
    assert body["quota"]["used_today"] == 1
    assert body["quota"]["remaining_today"] == 4

    assert body["tasks"]["total"] == 1
    assert body["tasks"]["by_status"]["succeeded"] == 1
    assert len(body["tasks"]["recent"]) == 1
    assert body["tasks"]["recent"][0]["kind"] == "text"

    assert len(body["devices"]) == 1
    assert body["devices"][0]["revoked"] is False
    assert body["devices"][0]["name"] == "pytest"

    assert body["usage"]["tokens_out"] >= 0


async def test_user_detail_404_for_unknown(client):
    response = await client.get("/internal/users/99999999", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_user_detail_reflects_revoked_device(client, new_user):
    username, password = await new_user()
    token = await login(client, username, password)
    me = (await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})).json()

    before = (
        await client.get(f"/internal/users/{me['id']}", headers=ADMIN)
    ).json()["devices"]
    assert [d["revoked"] for d in before] == [False]

    assert (
        await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 204

    after = (
        await client.get(f"/internal/users/{me['id']}", headers=ADMIN)
    ).json()["devices"]
    assert [d["revoked"] for d in after] == [True]
