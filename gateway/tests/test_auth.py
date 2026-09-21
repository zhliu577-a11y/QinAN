from __future__ import annotations

from app.core.security import sign_hmac_hex
from app.core.timeutil import unix_now

from .conftest import login


async def test_health(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["engine"] == "ready"
    assert body["pool_total"] == 1
    assert body["pool_idle"] == 1


async def test_token_login_success(client, user_a):
    username, password = user_a
    response = await client.post(
        "/api/v1/auth/token", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["username"] == username


async def test_token_login_wrong_password(client, user_a):
    username, _ = user_a
    response = await client.post(
        "/api/v1/auth/token", json={"username": username, "password": "nope"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_unknown_user_gets_same_error_as_wrong_password(client):
    response = await client.post(
        "/api/v1/auth/token", json={"username": "ghost", "password": "nope"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "用户名或密码错误"


async def test_me_requires_token(client):
    response = await client.get("/api/v1/me")
    assert response.status_code == 401


async def test_me_returns_quota(client, user_a, auth_headers_factory):
    token = await login(client, *user_a)
    response = await client.get("/api/v1/me", headers=auth_headers_factory(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == user_a[0]
    assert body["remaining_today"] <= body["daily_quota"]
    assert body["in_flight_limit"] == 1


async def test_refresh_issues_new_access_token(client, user_a):
    response = await client.post(
        "/api/v1/auth/token",
        json={"username": user_a[0], "password": user_a[1]},
    )
    refresh_token = response.json()["refresh_token"]
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]


async def test_refresh_rejects_unknown_token(client):
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "rt_does_not_exist"}
    )
    assert response.status_code == 401


async def test_logout_revokes_token(client, user_a, auth_headers_factory):
    token = await login(client, *user_a)
    headers = auth_headers_factory(token)
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 200

    logout = await client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204

    after = await client.get("/api/v1/me", headers=headers)
    assert after.status_code == 401


async def test_exchange_mode_b_creates_and_reuses_user(client):
    external_id = "u_ext_1001"
    timestamp = unix_now()
    signature = sign_hmac_hex(
        "test-exchange-secret", f"{external_id}.{timestamp}"
    )
    payload = {
        "external_user_id": external_id,
        "display_name": "外部用户",
        "timestamp": timestamp,
        "signature": signature,
    }
    first = await client.post("/api/v1/auth/exchange", json=payload)
    assert first.status_code == 200, first.text
    first_id = first.json()["user"]["id"]

    second = await client.post("/api/v1/auth/exchange", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["user"]["id"] == first_id


async def test_exchange_mode_b_rejects_bad_signature(client):
    timestamp = unix_now()
    response = await client.post(
        "/api/v1/auth/exchange",
        json={
            "external_user_id": "u_ext_bad",
            "timestamp": timestamp,
            "signature": "0" * 64,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "签名校验失败"


async def test_exchange_mode_b_rejects_stale_timestamp(client):
    external_id = "u_ext_stale"
    timestamp = unix_now() - 9999
    signature = sign_hmac_hex("test-exchange-secret", f"{external_id}.{timestamp}")
    response = await client.post(
        "/api/v1/auth/exchange",
        json={
            "external_user_id": external_id,
            "timestamp": timestamp,
            "signature": signature,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "签名时间戳超出允许范围"
