"""OpenAI 兼容的模拟模型服务，用于在没有外部模型的网络里验证整条消息链路。

为什么需要它：真实部署要到国内服务器上跑，那里连不到办公内网的模型网关；
但整条链路（开户 → 登录 → 提交 → 排队 → 派发 → opencode → 模型 → SSE → 结果）
又必须在没有外网模型的情况下可验证。这个服务补上最底下那一层。

它实现 opencode 真正会调用的最小接口：
  GET  /v1/models             列模型
  POST /v1/chat/completions   对话（支持 stream 与工具调用）
  GET  /health                探活

行为是确定性的，所以可以写自动化断言：
  - 提示词里带 URL 且工具里有 webfetch → 先回一个 webfetch 工具调用
  - 上一条是工具结果（role=tool）    → 回最终摘要，并带上"抓到的字数"
  - 其余情况                        → 直接按输入文本回一段结构化摘要

只能用 python 标准库：这里刻意不装任何依赖，构建快、在断网环境里也不会失败。

绝对不要把它放进生产环境：它不做鉴权、不理解语义、返回内容是编造的。
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8000
MODEL_ID = "deepseek-v4-flash"
MARKER = "【模拟模型】"

# 流式输出的切片大小与间隔，让客户端的增量渲染路径能被真实走一遍
CHUNK_CHARS = 24
CHUNK_DELAY_SECONDS = 0.01

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\)\]]+")
# prompting.build_prompt 会写 "URL: <url>"，优先用它，避免误抓任务说明里的其它链接
LABELED_URL_PATTERN = re.compile(r"URL:\s*(https?://[^\s\"'<>\)\]]+)")


def _text_of(content: Any) -> str:
    """把消息内容规整成纯文本。opencode 有时发字符串，有时发 parts 数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _tool_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        name = function.get("name") or tool.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _summarize(text: str, *, kind: str, extra: str = "") -> str:
    """按固定结构生成一段摘要。内容是编造的，但形状与真实输出一致。"""
    body = text.strip()
    head = body[:120].replace("\n", " ")
    bullet_source = [line.strip() for line in body.splitlines() if line.strip()]
    points = bullet_source[:3] or ["输入为空"]

    lines = [
        f"{MARKER}这是模拟模型生成的摘要，用于验证链路，未调用任何真实模型。",
        "",
        "## 要点",
    ]
    kind_label = "网页抓取" if kind == "url" else "文本"
    lines.append(f"- 任务类型：{kind_label}；输入正文长度约 {len(body)} 字。")
    for point in points:
        lines.append(f"- {point[:80]}")
    if extra:
        lines.append(f"- {extra}")
    lines.extend(["", "## 细节", f"输入开头：{head}" if head else "输入为空。"])
    return "\n".join(lines)


def _decide(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """决定这次该回什么。

    返回 (模式, 文本, 工具参数)。模式是 "text" 或 "tool"。
    """
    messages = payload.get("messages") or []
    tools = _tool_names(payload)

    tool_results = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    user_text = "\n".join(
        _text_of(m.get("content"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    )

    labeled = LABELED_URL_PATTERN.search(user_text)
    url = labeled.group(1) if labeled else None
    if url is None:
        found = URL_PATTERN.search(user_text)
        url = found.group(0) if found else None

    # 已经拿到抓取结果了，收尾输出摘要
    if tool_results:
        fetched = 0
        for message in tool_results:
            fetched += len(_text_of(message.get("content")))
        return (
            "text",
            _summarize(
                user_text,
                kind="url",
                extra=f"已通过 webfetch 抓取网页，工具结果长度 {fetched} 字。",
            ),
            None,
        )

    # 有 URL 且允许抓取 → 先发一个 webfetch 工具调用
    if url and "webfetch" in tools:
        arguments = json.dumps({"url": url, "format": "markdown"}, ensure_ascii=False)
        return "tool", None, arguments

    return "text", _summarize(user_text, kind="text"), None


def _completion_id() -> str:
    return f"chatcmpl-mock-{int(time.time() * 1000)}"


def _usage(text: str, payload: dict[str, Any]) -> dict[str, int]:
    prompt = sum(
        len(_text_of(m.get("content")))
        for m in payload.get("messages") or []
        if isinstance(m, dict)
    )
    return {
        "prompt_tokens": max(prompt // 2, 1),
        "completion_tokens": max(len(text) // 2, 1),
        "total_tokens": max((prompt + len(text)) // 2, 2),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockModel/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 统一走 stderr，便于 docker logs 查看
        sys.stderr.write("[mock-model] " + fmt % args + "\n")
        sys.stderr.flush()

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", "/global/health"):
            self._send_json({"status": "ok", "service": "mock-model"})
            return
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "mock",
                        }
                    ],
                }
            )
            return
        self._send_json({"error": {"message": f"unknown path {self.path}"}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json({"error": {"message": f"unknown path {self.path}"}}, status=404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send_json({"error": {"message": f"bad json: {exc}"}}, status=400)
            return

        mode, text, arguments = _decide(payload)
        stream = bool(payload.get("stream"))
        self.log_message(
            "completions stream=%s mode=%s model=%s", stream, mode, payload.get("model")
        )

        if stream:
            self._send_stream(payload, mode, text, arguments)
        else:
            self._send_json(self._build_body(payload, mode, text, arguments))

    # ---------- 响应构造 ----------

    def _build_body(
        self, payload: dict[str, Any], mode: str, text: str | None, arguments: str | None
    ) -> dict[str, Any]:
        if mode == "tool":
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_mock_1",
                        "type": "function",
                        "function": {"name": "webfetch", "arguments": arguments or "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": text or ""}
            finish = "stop"

        return {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or MODEL_ID,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": _usage(text or "", payload),
        }

    def _chunk(
        self, payload: dict[str, Any], delta: dict[str, Any], finish: str | None
    ) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "id": _completion_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload.get("model") or MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return chunk

    def _send_stream(
        self, payload: dict[str, Any], mode: str, text: str | None, arguments: str | None
    ) -> None:
        # SSE 没有 Content-Length，不显式关闭连接的话客户端无法判断正文到此结束
        # （实测会在读完 [DONE] 后一直阻塞到超时）。这里用 Connection: close
        # 让响应体的边界就是连接关闭，比手写 chunked 编码更不容易出错。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj: Any) -> None:
            line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        emit(self._chunk(payload, {"role": "assistant", "content": ""}, None))

        if mode == "tool":
            emit(
                self._chunk(
                    payload,
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_mock_1",
                                "type": "function",
                                "function": {"name": "webfetch", "arguments": arguments or "{}"},
                            }
                        ]
                    },
                    None,
                )
            )
            emit(self._chunk(payload, {}, "tool_calls"))
        else:
            body = text or ""
            for start in range(0, len(body), CHUNK_CHARS):
                emit(self._chunk(payload, {"content": body[start : start + CHUNK_CHARS]}, None))
                time.sleep(CHUNK_DELAY_SECONDS)
            final = self._chunk(payload, {}, "stop")
            final["usage"] = _usage(body, payload)
            emit(final)

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------- 工具 ----------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[mock-model] listening on :{PORT}\n")
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
