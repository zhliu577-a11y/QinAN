from __future__ import annotations

import asyncio
import time

from .conftest import login

TERMINAL = {"succeeded", "failed", "canceled", "timeout"}


async def wait_terminal(client, headers, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态，最后状态={last}")


async def test_url_task_succeeds(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)

    created = await client.post(
        "/api/v1/tasks",
        json={"kind": "url", "url": "https://example.com/article"},
        headers={**headers, "Idempotency-Key": "task-url-1"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["queue_pos"] >= 1

    final = await wait_terminal(client, headers, body["task_id"])
    assert final["status"] == "succeeded", final
    assert final["result_md"] and "摘要" in final["result_md"]
    assert final["usage"]["tokens_in"] > 0
    assert final["usage"]["duration_ms"] >= 0
    assert final["source"]["url"] == "https://example.com/article"
    assert final["error"] is None


async def test_text_task_succeeds(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)

    created = await client.post(
        "/api/v1/tasks",
        json={
            "kind": "text",
            "text": "这是一段需要被总结的文本，包含若干要点。",
            "instruction": "提炼三句话",
            "max_output_chars": 500,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    final = await wait_terminal(client, headers, created.json()["task_id"])
    assert final["status"] == "succeeded", final
    assert final["kind"] == "text"
    assert final["result_md"]
    assert final["source"]["fetched_chars"] == len("这是一段需要被总结的文本，包含若干要点。")


async def test_idempotency_key_returns_same_task(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = {**auth_headers_factory(token), "Idempotency-Key": "idem-fixed-1"}
    payload = {"kind": "text", "text": "重复提交测试"}

    first = await client.post("/api/v1/tasks", json=payload, headers=headers)
    second = await client.post("/api/v1/tasks", json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["task_id"] == second.json()["task_id"]


async def test_invalid_url_rejected(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)

    missing = await client.post("/api/v1/tasks", json={"kind": "url"}, headers=headers)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "INVALID_INPUT"

    bad_scheme = await client.post(
        "/api/v1/tasks",
        json={"kind": "url", "url": "file:///etc/passwd"},
        headers=headers,
    )
    assert bad_scheme.status_code == 400
    assert "http(s)" in bad_scheme.json()["error"]["message"]


async def test_empty_text_rejected(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    response = await client.post(
        "/api/v1/tasks",
        json={"kind": "text", "text": "   "},
        headers=auth_headers_factory(token),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_INPUT"


async def test_callback_url_must_be_allowlisted(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)

    rejected = await client.post(
        "/api/v1/tasks",
        json={
            "kind": "text",
            "text": "回调白名单测试",
            "callback_url": "https://evil.example.net/hook",
        },
        headers=headers,
    )
    assert rejected.status_code == 400
    assert "callback_url" in rejected.json()["error"]["message"]

    allowed = await client.post(
        "/api/v1/tasks",
        json={
            "kind": "text",
            "text": "回调白名单测试",
            "callback_url": "https://app.example.com/hook",
        },
        headers=headers,
    )
    assert allowed.status_code == 201, allowed.text


async def test_cross_user_task_access_forbidden(
    client, new_user, auth_headers_factory
):
    alice = await new_user()
    bob = await new_user()
    alice_headers = auth_headers_factory(await login(client, *alice))
    bob_headers = auth_headers_factory(await login(client, *bob))

    created = await client.post(
        "/api/v1/tasks",
        json={"kind": "text", "text": "越权测试"},
        headers=alice_headers,
    )
    task_id = created.json()["task_id"]

    response = await client.get(f"/api/v1/tasks/{task_id}", headers=bob_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"

    cancel = await client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=bob_headers
    )
    assert cancel.status_code == 403


async def test_unknown_task_not_found(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    response = await client.get(
        "/api/v1/tasks/tsk_does_not_exist", headers=auth_headers_factory(token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_task_list_only_returns_own_tasks(
    client, new_user, auth_headers_factory
):
    alice = await new_user()
    bob = await new_user()
    alice_headers = auth_headers_factory(await login(client, *alice))
    bob_headers = auth_headers_factory(await login(client, *bob))

    for index in range(2):
        await client.post(
            "/api/v1/tasks",
            json={"kind": "text", "text": f"alice {index}"},
            headers={**alice_headers, "Idempotency-Key": f"list-{index}"},
        )
    await wait_terminal(
        client,
        alice_headers,
        (
            await client.get("/api/v1/tasks", headers=alice_headers)
        ).json()["items"][0]["task_id"],
    )

    alice_list = await client.get("/api/v1/tasks", headers=alice_headers)
    bob_list = await client.get("/api/v1/tasks", headers=bob_headers)
    assert alice_list.status_code == 200
    assert len(alice_list.json()["items"]) >= 2
    assert bob_list.json()["items"] == []


async def test_quota_exceeded(client, new_user, auth_headers_factory, runtime):
    from app.models import User

    username, password = await new_user(quota=0)
    token = await login(client, username, password)
    headers = auth_headers_factory(token)

    response = await client.post(
        "/api/v1/tasks",
        json={"kind": "text", "text": "配额测试"},
        headers=headers,
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "QUOTA_EXCEEDED"

    async with runtime.db_session() as db:
        from sqlalchemy import select

        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        user.daily_quota = 30
        await db.commit()


async def test_in_flight_limit_returns_queue_full(
    client, new_user, auth_headers_factory, runtime
):
    instance = runtime.pool.instances[0]
    original_delay = instance.client._step_delay
    instance.client._step_delay = 2.0
    try:
        token = await login(client, *(await new_user()))
        headers = auth_headers_factory(token)

        first = await client.post(
            "/api/v1/tasks",
            json={"kind": "text", "text": "慢任务一"},
            headers=headers,
        )
        assert first.status_code == 201, first.text

        await asyncio.sleep(0.4)
        second = await client.post(
            "/api/v1/tasks",
            json={"kind": "text", "text": "慢任务二"},
            headers=headers,
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "QUEUE_FULL"
        assert second.json()["error"]["retry_after"] > 0

        cancel = await client.post(
            f"/api/v1/tasks/{first.json()['task_id']}/cancel", headers=headers
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "canceled"
    finally:
        instance.client._step_delay = original_delay


async def test_cancel_terminal_task_conflicts(
    client, new_user, auth_headers_factory
):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)
    created = await client.post(
        "/api/v1/tasks", json={"kind": "text", "text": "取消终态"}, headers=headers
    )
    task_id = created.json()["task_id"]
    await wait_terminal(client, headers, task_id)

    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_CANCELABLE"


async def test_login_required_for_tasks(client):
    response = await client.post(
        "/api/v1/tasks", json={"kind": "text", "text": "未鉴权"}
    )
    assert response.status_code == 401
