"""端到端验证：App 的客户端代码 -> 本机真实网关（MOCK_MODE）。

起一个 uvicorn（只绑 127.0.0.1 的随机端口），建一个演示用户，然后跑 AppE2E。
跑完就把 uvicorn 杀掉，不碰任何内网/对外端口，也不需要 opencode 和模型凭据。

用法（在仓库根目录，用项目自己的 venv）：
    .\\.venv\\Scripts\\python.exe android\\tools\\e2e-against-gateway.py
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOOLS = pathlib.Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
GATEWAY = ROOT / "gateway"
WORK = ROOT / ".tmp" / "e2e"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
JSON_JAR = ROOT / "android" / "build" / "libs" / "json-20240303.jar"
AGENT_SRC = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "qinan" / "agent"

ADMIN_TOKEN = "e2e-admin-token"
USERNAME = "e2e-app"
PASSWORD = "e2e-app-password"

ENV = {
    "MOCK_MODE": "true",
    "MOCK_STEP_DELAY_SECONDS": "0.05",
    "JWT_SECRET": "e2e-secret-value-that-is-long-enough",
    "DATABASE_URL": "sqlite:///./e2e.db",
    "ADMIN_TOKEN": ADMIN_TOKEN,
    "EXCHANGE_HMAC_SECRET": "e2e-exchange-secret",
    "CALLBACK_HMAC_SECRET": "e2e-callback-secret",
    "CALLBACK_ALLOWED_HOSTS": "app.example.com",
    "DEFAULT_DAILY_QUOTA": "30",
    "MAX_IN_FLIGHT_PER_USER": "1",
    "MAX_QUEUE_DEPTH_PER_USER": "3",
    "TASK_TIMEOUT_SECONDS": "60",
    "DISPATCHER_INTERVAL_SECONDS": "0.1",
    "POOL_HEALTH_INTERVAL_SECONDS": "3600",
    "INSTANCE_IDLE_DISPOSE_MINUTES": "0",
    "SSE_PING_INTERVAL_SECONDS": "2",
    "LOG_LEVEL": "WARNING",
    "MODEL_PROVIDER": "deepseek",
    "MODEL_NAME": "deepseek-flash",
}


def find_java_home() -> pathlib.Path:
    candidates = [os.environ.get("JAVA_HOME")]
    candidates += [
        str(path)
        for root in (
            r"C:\Program Files\Eclipse Adoptium",
            r"C:\Program Files\Java",
            r"C:\Program Files\Android\Android Studio\jbr",
        )
        if pathlib.Path(root).is_dir()
        for path in sorted(pathlib.Path(root).iterdir(), reverse=True)
    ]
    for candidate in candidates:
        if candidate and (pathlib.Path(candidate) / "bin" / "javac.exe").is_file():
            return pathlib.Path(candidate)
    raise SystemExit("找不到 JDK，先设 JAVA_HOME")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(base: str, proc: subprocess.Popen, seconds: float = 40.0) -> dict:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"网关进程提前退出，退出码 {proc.returncode}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
            time.sleep(0.5)
    raise SystemExit(f"等 /health 超时：{last}")


def post(url: str, body: dict, headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def run(cmd: list[str]) -> None:
    print("$ " + " ".join(str(part) for part in cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"命令失败，退出码 {result.returncode}")


def main() -> None:
    java_home = find_java_home()
    WORK.mkdir(parents=True, exist_ok=True)
    db = WORK / "e2e.db"
    if db.exists():
        db.unlink()

    # org.json 在桌面上不是 JDK 自带的；正常由 build.ps1 的冒烟测试步骤下好。
    if not JSON_JAR.exists():
        JSON_JAR.parent.mkdir(parents=True, exist_ok=True)
        url = "https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar"
        print(f"下载 {url}")
        urllib.request.urlretrieve(url, JSON_JAR)

    port = free_port()
    base_root = f"http://127.0.0.1:{port}"
    base = base_root + "/api/v1"

    env = os.environ.copy()
    env.update(ENV)
    env["PYTHONPATH"] = str(GATEWAY)
    env["PYTHONIOENCODING"] = "utf-8"
    python = str(VENV_PY if VENV_PY.exists() else sys.executable)

    print(f"启动网关（cwd={WORK}，只绑 127.0.0.1:{port}）…")
    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(WORK),
        env=env,
    )
    try:
        health = wait_health(base, proc)
        print(f"/health -> {health}")

        status, text = post(
            base_root + "/internal/users",
            {"username": USERNAME, "password": PASSWORD, "daily_quota": 30},
            {"X-Admin-Token": ADMIN_TOKEN},
        )
        print(f"建用户 -> HTTP {status} {text}")
        if status not in (201, 400):
            raise SystemExit("建用户失败")

        # 用 App 里那两个类编译（没有 android.jar，正好也证明它们与平台无关）。
        classes = WORK / "classes"
        classes.mkdir(parents=True, exist_ok=True)
        sources = [AGENT_SRC / "ApiClient.java", AGENT_SRC / "TaskPayload.java", TOOLS / "AppE2E.java"]
        run(
            [
                str(java_home / "bin" / "javac.exe"),
                "-encoding", "UTF-8",
                "-nowarn",
                "-d", str(classes),
                "-cp", str(JSON_JAR),
                *[str(source) for source in sources],
            ]
        )
        run(
            [
                str(java_home / "bin" / "java.exe"),
                "-Dfile.encoding=UTF-8",
                "-cp", f"{classes}{os.pathsep}{JSON_JAR}",
                "com.qinan.agent.AppE2E",
                base,
                USERNAME,
                PASSWORD,
            ]
        )
    finally:
        print("停网关…")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"网关退出码 {proc.returncode}（terminate 导致的非 0 是正常的）")


if __name__ == "__main__":
    main()
