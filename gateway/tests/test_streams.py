from __future__ import annotations

import asyncio

from app.api.streams import task_websocket

from .conftest import login
from .test_tasks import wait_terminal


async def _prepare_finished_task(client, new_user, auth_headers_factory):
    token = await login(client, *(await new_user()))
    headers = auth_headers_factory(token)
    created = await client.post(
        "/api/v1/tasks",
        json={"kind": "text", "text": "流式输出测试文本"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    await wait_terminal(client, headers, task_id)
    return token, headers, task_id


async def test_sse_replays_history_and_closes(
    client, new_user, auth_headers_factory
):
    token, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )

    events: list[str] = []
    data_lines: list[str] = []
    async with client.stream(
        "GET", f"/api/v1/tasks/{task_id}/events", params={"token": token}
    ) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
            if events and events[-1] == "done":
                break

    assert "delta" in events
    assert events[-1] == "done"
    assert all('"type"' in line for line in data_lines)


async def test_sse_last_seq_skips_already_seen_events(
    client, new_user, auth_headers_factory
):
    token, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )

    async def collect(last_seq: int) -> list[int]:
        ids: list[int] = []
        async with client.stream(
            "GET",
            f"/api/v1/tasks/{task_id}/events",
            params={"token": token, "last_seq": last_seq},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    ids.append(int(line[len("id: ") :]))
                if line.startswith("event: done"):
                    break
        return ids

    all_ids = await collect(0)
    assert len(all_ids) >= 2
    assert all_ids == sorted(all_ids)

    cutoff = all_ids[0]
    remaining = await collect(cutoff)
    assert remaining
    assert min(remaining) > cutoff


async def test_sse_requires_token(client, new_user, auth_headers_factory):
    _, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )
    response = await client.get(f"/api/v1/tasks/{task_id}/events")
    assert response.status_code == 401


class StubWebSocket:
    """只实现处理函数用到的那部分接口，避免引入真实 WS 服务器。"""

    def __init__(self, token: str | None) -> None:
        self.query_params = {"token": token} if token else {}
        self.headers: dict[str, str] = {}
        self.sent: list[dict] = []
        self.closed_code: int | None = None
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)
        raise AssertionError("不应被调用")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_code = code


async def test_websocket_streams_events(client, new_user, auth_headers_factory):
    token, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )
    websocket = StubWebSocket(token)

    async with asyncio.timeout(10):
        await task_websocket(websocket, task_id)  # type: ignore[arg-type]

    assert websocket.accepted
    types = [message["type"] for message in websocket.sent]
    assert "delta" in types
    assert types[-1] == "done"
    assert websocket.closed_code == 1000


async def test_websocket_rejects_missing_token(client, new_user, auth_headers_factory):
    _, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )
    websocket = StubWebSocket(None)

    await task_websocket(websocket, task_id)  # type: ignore[arg-type]

    assert not websocket.accepted
    assert websocket.closed_code == 4401


async def test_websocket_rejects_other_users_task(
    client, new_user, auth_headers_factory
):
    _, _, task_id = await _prepare_finished_task(
        client, new_user, auth_headers_factory
    )
    intruder = await new_user()
    intruder_token = await login(client, *intruder)
    websocket = StubWebSocket(intruder_token)

    await task_websocket(websocket, task_id)  # type: ignore[arg-type]

    assert not websocket.accepted
    assert websocket.closed_code == 4401
