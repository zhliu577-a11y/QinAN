"""鉴权与公共依赖。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..core.errors import forbidden, unauthorized
from ..core.security import decode_access_token
from ..models import ApiClient, User
from ..runtime import Runtime, get_runtime

DbSession = Annotated[AsyncSession, Depends(get_db)]


@dataclass
class AuthContext:
    user: User
    client: ApiClient


def runtime_dep() -> Runtime:
    return get_runtime()


async def _load_context(
    db: AsyncSession, token: str | None
) -> AuthContext:
    if not token:
        raise unauthorized()
    payload = decode_access_token(token)
    if not payload:
        raise unauthorized()
    subject = payload.get("sub")
    client_id = payload.get("cid")
    if not subject or not client_id:
        raise unauthorized()

    client = await db.get(ApiClient, str(client_id))
    if client is None or client.revoked_at is not None:
        raise unauthorized()
    if str(client.user_id) != str(subject):
        raise unauthorized()

    user = await db.get(User, client.user_id)
    if user is None or not user.enabled:
        raise unauthorized("账号已被禁用")
    return AuthContext(user=user, client=client)


def _extract_bearer(authorization: Optional[str]) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def current_auth(
    db: DbSession,
    authorization: Annotated[Optional[str], Header()] = None,
    token: Annotated[Optional[str], Query()] = None,
) -> AuthContext:
    """同时支持 Authorization 头与 ?token= 查询参数。

    查询参数是为 SSE / WebSocket 准备的：EventSource 与部分移动端
    客户端无法自定义请求头。
    """
    return await _load_context(db, _extract_bearer(authorization) or token)


CurrentAuth = Annotated[AuthContext, Depends(current_auth)]


async def require_admin_token(
    runtime: Annotated[Runtime, Depends(runtime_dep)],
    x_admin_token: Annotated[Optional[str], Header()] = None,
) -> None:
    expected = runtime.settings.admin_token
    if not expected:
        raise forbidden("管理接口未启用")
    if x_admin_token != expected:
        raise forbidden("管理令牌不正确")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
