"""测试夹具。

环境变量必须在导入 app 之前设置：Settings 与 Runtime 都是进程级单例。
"""

from __future__ import annotations

import os
import secrets
import tempfile
from itertools import count
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TMP_DIR = Path(tempfile.mkdtemp(prefix="gateway-tests-"))

os.environ.update(
    {
        "MOCK_MODE": "true",
        "MOCK_STEP_DELAY_SECONDS": "0.01",
        "JWT_SECRET": "test-secret-value-that-is-long-enough",
        "DATABASE_URL": f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}",
        "ADMIN_TOKEN": "test-admin-token",
        "EXCHANGE_HMAC_SECRET": "test-exchange-secret",
        "CALLBACK_HMAC_SECRET": "test-callback-secret",
        "CALLBACK_ALLOWED_HOSTS": "app.example.com",
        "DEFAULT_DAILY_QUOTA": "30",
        "MAX_IN_FLIGHT_PER_USER": "1",
        "MAX_QUEUE_DEPTH_PER_USER": "3",
        "TASK_TIMEOUT_SECONDS": "30",
        "DISPATCHER_INTERVAL_SECONDS": "0.05",
        "POOL_HEALTH_INTERVAL_SECONDS": "3600",
        "INSTANCE_IDLE_DISPOSE_MINUTES": "0",
        "SSE_PING_INTERVAL_SECONDS": "2",
        "LOG_LEVEL": "WARNING",
        "MODEL_PROVIDER": "deepseek",
        "MODEL_NAME": "deepseek-chat",
    }
)

from app.core.config import get_settings  # noqa: E402
from app.core.db import get_sessionmaker  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.runtime import get_runtime, reset_runtime  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def runtime():
    get_settings.cache_clear()
    await reset_runtime()
    instance = get_runtime()
    await instance.start()
    yield instance
    await instance.stop()


@pytest_asyncio.fixture
async def client(runtime):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def session_factory(runtime):
    return get_sessionmaker()


async def _ensure_user(username: str, password: str, quota: int = 30) -> None:
    async with get_sessionmaker()() as db:
        from sqlalchemy import select

        found = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if found is None:
            db.add(
                User(
                    username=username,
                    display_name=username,
                    password_hash=hash_password(password),
                    daily_quota=quota,
                )
            )
            await db.commit()


@pytest_asyncio.fixture
async def user_a(runtime):
    await _ensure_user("alice", "alice-password")
    return ("alice", "alice-password")


@pytest_asyncio.fixture
async def user_b(runtime):
    await _ensure_user("bob", "bob-password")
    return ("bob", "bob-password")


@pytest_asyncio.fixture
async def new_user(runtime):
    """每个测试用独立账号，避免配额与任务数互相污染。"""
    counter = count(1)

    async def _make(quota: int = 30) -> tuple[str, str]:
        name = f"u{next(counter)}_{secrets.token_hex(3)}"
        password = f"pw-{secrets.token_hex(4)}"
        await _ensure_user(name, password, quota)
        return name, password

    return _make


async def login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/api/v1/auth/token",
        json={"username": username, "password": password, "device_id": "pytest"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth_headers_factory():
    def build(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return build
