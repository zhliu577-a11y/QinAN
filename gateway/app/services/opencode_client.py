"""opencode 服务端客户端，以及行为等价的 MOCK 实现。

两者接口一致，因此调度器与事件流转发在 MOCK_MODE 下走的是同一套代码路径，
沙箱里验证过的状态流转在生产环境同样成立。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.config import InstanceSpec
from .prompting import SAFE_TOOLS

logger = logging.getLogger(__name__)


class OpencodeError(RuntimeError):
    pass


class OpencodeClient:
    def __init__(self, spec: InstanceSpec, username: str, timeout_seconds: int = 30) -> None:
        self.spec = spec
        self.instance_id = spec.id
        self.base_url = spec.base_url
        self._auth = httpx.BasicAuth(username, spec.password)
        self._timeout = httpx.Timeout(
            connect=5.0, read=float(timeout_seconds), write=30.0, pool=5.0
        )
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url, auth=self._auth, timeout=self._timeout
            )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise OpencodeError(f"实例 {self.instance_id} 客户端未启动")
        return self._http

    # ---------- 基础操作 ----------

    async def health(self) -> bool:
        try:
            response = await self.http.get("/global/health", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    async def create_session(self, title: str | None = None) -> str:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title[:120]
        response = await self.http.post("/session", json=payload)
        if response.status_code >= 400:
            raise OpencodeError(
                f"创建会话失败 {response.status_code}: {response.text[:200]}"
            )
        data = response.json()
        session_id = data.get("id")
        if not session_id:
            raise OpencodeError("创建会话响应缺少 id")
        return str(session_id)

    async def prompt_async(
        self,
        session_id: str,
        prompt: str,
        *,
        agent: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "tools": dict(SAFE_TOOLS),
        }
        if agent:
            payload["agent"] = agent
        if provider_id and model_id:
            payload["model"] = {"providerID": provider_id, "modelID": model_id}
        response = await self.http.post(
            f"/session/{session_id}/prompt_async", json=payload
        )
        if response.status_code >= 400:
            raise OpencodeError(
                f"下发任务失败 {response.status_code}: {response.text[:200]}"
            )

    async def abort(self, session_id: str) -> None:
        try:
            await self.http.post(f"/session/{session_id}/abort", timeout=10.0)
        except Exception as exc:  # abort 失败不应影响任务状态落库
            logger.warning("abort 会话失败 session=%s: %s", session_id, exc)

    async def dispose(self) -> None:
        try:
            await self.http.post("/instance/dispose", timeout=10.0)
        except Exception as exc:
            logger.warning("dispose 实例失败 instance=%s: %s", self.instance_id, exc)

    async def last_assistant_message(self, session_id: str) -> dict[str, Any] | None:
        """取最后一条 assistant 消息，用于兜底解析结果与用量。"""
        try:
            response = await self.http.get(
                f"/session/{session_id}/message", params={"limit": 10}
            )
        except Exception as exc:
            logger.warning("拉取消息失败 session=%s: %s", session_id, exc)
            return None
        if response.status_code >= 400:
            return None
        try:
            items = response.json()
        except ValueError:
            return None
        for item in reversed(items or []):
            info = item.get("info") or {}
            if info.get("role") == "assistant":
                text = "".join(
                    part.get("text", "")
                    for part in item.get("parts") or []
                    if part.get("type") == "text"
                )
                return {"info": info, "text": text}
        return None

    # ---------- 事件流 ----------

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        """订阅 /event。read 超时设为无限，靠 opencode 的 10s 心跳保活。"""
        timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
        async with self.http.stream("GET", "/event", timeout=timeout) as response:
            if response.status_code >= 400:
                raise OpencodeError(f"订阅事件流失败: HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue


MOCK_CHUNK = 14
# 真实 opencode 同样会把思维链作为 part 推流，mock 保留一段用于验证转发层确实把它过滤掉了
MOCK_REASONING_TEXT = "（内部推理）先判断输入是链接还是文本，再压缩成要点。"


class MockOpencodeClient:
    """沙箱实现：不依赖真实 agent，按固定节奏产出与真实事件同名的事件。"""

    def __init__(self, instance_id: str, step_delay: float = 0.6) -> None:
        self.instance_id = instance_id
        self.base_url = "mock://local"
        self._step_delay = max(step_delay, 0.0)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._producers: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, str] = {}
        self._usage: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        for task in list(self._producers.values()):
            task.cancel()
        self._producers.clear()

    async def health(self) -> bool:
        return True

    async def create_session(self, title: str | None = None) -> str:
        return f"ses_mock_{secrets.token_hex(6)}"

    async def prompt_async(
        self,
        session_id: str,
        prompt: str,
        *,
        agent: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        existing = self._producers.pop(session_id, None)
        if existing is not None:
            existing.cancel()
        task = asyncio.create_task(self._produce(session_id, prompt, agent))
        self._producers[session_id] = task

    async def _produce(self, session_id: str, prompt: str, agent: str | None) -> None:
        try:
            await self._put("session.status", session_id, {"type": "busy"})
            await asyncio.sleep(self._step_delay)

            result = self._render_result(prompt, agent)
            self._results[session_id] = result
            message_id = f"msg_{secrets.token_hex(6)}"
            reasoning_id = f"prt_{secrets.token_hex(6)}"
            part_id = f"prt_{secrets.token_hex(6)}"

            await self._stream_part(
                session_id, message_id, reasoning_id, "reasoning", MOCK_REASONING_TEXT
            )
            await self._stream_part(session_id, message_id, part_id, "text", result)

            usage = {
                "tokens_in": max(64, len(prompt) // 3),
                "tokens_out": max(32, len(result) // 3),
            }
            self._usage[session_id] = usage
            await self._put(
                "message.updated",
                session_id,
                {
                    "info": {
                        "id": message_id,
                        "sessionID": session_id,
                        "role": "assistant",
                        "time": {"created": 0, "completed": 1},
                        "tokens": {
                            "input": usage["tokens_in"],
                            "output": usage["tokens_out"],
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                        "cost": 0.0,
                    }
                },
            )
            await asyncio.sleep(self._step_delay / 2)
            await self._put("session.idle", session_id, {})
        except asyncio.CancelledError:
            raise

    async def _stream_part(
        self, session_id: str, message_id: str, part_id: str, part_type: str, text: str
    ) -> None:
        """先宣告 part 类型，再用 message.part.delta 推增量，与 opencode 1.18 的实际事件一致。"""
        await self._put(
            "message.part.updated",
            session_id,
            {
                "part": {
                    "id": part_id,
                    "sessionID": session_id,
                    "messageID": message_id,
                    "type": part_type,
                    "text": "",
                }
            },
        )
        for index in range(0, len(text), MOCK_CHUNK):
            await self._put(
                "message.part.delta",
                session_id,
                {
                    "messageID": message_id,
                    "partID": part_id,
                    "field": "text",
                    "delta": text[index : index + MOCK_CHUNK],
                },
            )
            await asyncio.sleep(self._step_delay / 2)

    async def _put(self, event_type: str, session_id: str, extra: dict[str, Any]) -> None:
        properties: dict[str, Any] = {"sessionID": session_id}
        properties.update(extra)
        await self._queue.put({"type": event_type, "properties": properties})

    def _render_result(self, prompt: str, agent: str | None) -> str:
        kind = "链接" if "URL:" in prompt else "文本"
        return (
            "# 摘要（沙箱样例）\n\n"
            "## 结论\n"
            f"这是 MOCK 模式返回的固定样例，用于联调任务{kind}摘要流程。\n\n"
            "## 要点\n"
            f"- 调用方 agent：{agent or 'default'}\n"
            f"- 收到 prompt 长度：{len(prompt)} 字符\n"
            "- 状态流转：queued → running → streaming → succeeded\n"
            "- 真实环境会返回模型对目标内容的总结\n\n"
            "## 细节\n"
            "沙箱只验证接口契约，不产生真实模型调用与费用。\n"
        )

    async def abort(self, session_id: str) -> None:
        task = self._producers.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def dispose(self) -> None:
        return None

    async def last_assistant_message(self, session_id: str) -> dict[str, Any] | None:
        text = self._results.get(session_id)
        if text is None:
            return None
        usage = self._usage.get(session_id, {"tokens_in": 0, "tokens_out": 0})
        return {
            "info": {
                "role": "assistant",
                "time": {"created": 0, "completed": 1},
                "tokens": {
                    "input": usage["tokens_in"],
                    "output": usage["tokens_out"],
                },
                "cost": 0.0,
            },
            "text": text,
        }

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "server.connected", "properties": {}}
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=10.0)
            except TimeoutError:
                yield {"type": "server.heartbeat", "properties": {}}
                continue
            yield event
