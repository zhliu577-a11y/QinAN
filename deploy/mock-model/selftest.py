"""模拟模型服务的自测：直接跑，不需要 docker、不需要任何依赖。

    python selftest.py

覆盖两条会真实影响链路的路径：文本任务的直接摘要、URL 任务的
"先发 webfetch 工具调用 → 再基于工具结果收尾" 两步。流式与非流式都验证。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PORT = 8731
BASE = f"http://127.0.0.1:{PORT}"
WEBFETCH_TOOL = {"type": "function", "function": {"name": "webfetch"}}


def _start_server() -> ThreadingHTTPServer:
    path = Path(__file__).with_name("server.py")
    spec = importlib.util.spec_from_file_location("mock_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(payload: dict, *, stream: bool = False):
    request = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        if not stream:
            return json.loads(response.read().decode("utf-8"))
        lines: list[str] = []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("data: "):
                lines.append(line[len("data: ") :])
        return lines


def main() -> int:
    server = _start_server()
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            print("FAIL: 服务没起来")
            return 1

        print("== /v1/models ==")
        with urllib.request.urlopen(BASE + "/v1/models", timeout=5) as response:
            models = json.loads(response.read().decode("utf-8"))
        ids = [item["id"] for item in models["data"]]
        print("   ", ids)
        assert ids, "模型列表为空"

        print("== 文本任务：直接回摘要 ==")
        body = _post(
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": "你是资料摘要助手。"},
                    {
                        "role": "user",
                        "content": "## 本次任务\n请对下面的文本输出摘要。\n\n深度学习推理时延受批大小影响。",
                    },
                ],
                "tools": [WEBFETCH_TOOL],
            }
        )
        choice = body["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert not choice["message"].get("tool_calls")
        assert choice["message"]["content"].startswith("【模拟模型】")
        assert body["usage"]["total_tokens"] > 0
        print("    finish_reason=stop, usage=", body["usage"])

        print("== URL 任务第一步：应发出 webfetch 工具调用 ==")
        url_messages = [
            {
                "role": "user",
                "content": (
                    "## 本次任务\n请使用 webfetch 抓取下面的网页，然后输出摘要。\n"
                    "URL: https://example.com/a\n\n## 要求\n- 输出要点"
                ),
            }
        ]
        body = _post(
            {
                "model": "deepseek-v4-flash",
                "messages": url_messages,
                "tools": [WEBFETCH_TOOL],
            }
        )
        choice = body["choices"][0]
        calls = choice["message"].get("tool_calls") or []
        assert choice["finish_reason"] == "tool_calls", choice["finish_reason"]
        assert calls and calls[0]["function"]["name"] == "webfetch"
        arguments = json.loads(calls[0]["function"]["arguments"])
        assert arguments["url"] == "https://example.com/a", arguments
        print("    tool_calls=", json.dumps(calls, ensure_ascii=False))

        # 任务说明里没有 URL 时不该误抓 —— 否则纯文本任务会凭空发起抓取
        print("== 文本任务即便带 webfetch 工具也不该发工具调用 ==")
        body = _post(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "## 本次任务\n请总结这段文字。"}],
                "tools": [WEBFETCH_TOOL],
            }
        )
        assert body["choices"][0]["finish_reason"] == "stop"
        print("    ok")

        print("== URL 任务第二步：流式输出最终摘要 ==")
        lines = _post(
            {
                "model": "deepseek-v4-flash",
                "stream": True,
                "messages": url_messages
                + [
                    {"role": "assistant", "content": None, "tool_calls": calls},
                    {
                        "role": "tool",
                        "tool_call_id": "call_mock_1",
                        "content": "# Example\n" + "正文内容" * 200,
                    },
                ],
                "tools": [WEBFETCH_TOOL],
            },
            stream=True,
        )
        assert lines[-1] == "[DONE]", lines[-1]
        chunks = [json.loads(line) for line in lines[:-1]]
        text = "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert "抓取网页" in text, text[:200]
        assert text.startswith("【模拟模型】")
        print(f"    chunks={len(chunks)}, usage={chunks[-1].get('usage')}")

        print()
        print("PASS: 模拟模型服务全部检查通过")
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
