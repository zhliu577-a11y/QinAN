"""身份：模式 A（账号密码换 Token）与模式 B（App 侧 HMAC 直传）。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import forbidden, unauthorized
from ..core.security import (
    constant_time_equals,
    create_access_token,
    hash_token,
    new_id,
    new_opaque_token,
    sign_hmac_hex,
    verify_password,
)
from ..core.timeutil import unix_now, utcnow
from ..models import ApiClient, User
from ..runtime import Runtime
from ..schemas import (
    ExchangeRequest,
    RefreshRequest,
    RefreshResponse,
    TokenRequest,
    TokenResponse,
    UserOut,
)
from .deps import CurrentAuth, DbSession, runtime_dep

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        daily_quota=user.daily_quota,
    )


async def _issue_tokens(db: AsyncSession, user: User, name: str) -> TokenResponse:
    client_id = new_id("cli")
    refresh_token = new_opaque_token()
    db.add(
        ApiClient(
            id=client_id,
            user_id=user.id,
            name=name[:64],
            refresh_token_hash=hash_token(refresh_token),
            last_used_at=utcnow(),
        )
    )
    await db.commit()
    access_token, ttl = create_access_token(user.id, client_id)
    return TokenResponse(
        access_token=access_token,
        expires_in=ttl,
        refresh_token=refresh_token,
        user=_user_out(user),
    )


@router.post("/token", response_model=TokenResponse)
async def issue_token(
    payload: TokenRequest,
    db: DbSession,
) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        # 不区分「用户不存在」与「密码错误」，避免账号枚举
        raise unauthorized("用户名或密码错误")
    if not user.enabled:
        raise unauthorized("账号已被禁用")
    return await _issue_tokens(db, user, payload.device_id or "")


@router.post("/exchange", response_model=TokenResponse)
async def exchange_token(
    payload: ExchangeRequest,
    db: DbSession,
    runtime: Annotated[Runtime, Depends(runtime_dep)],
) -> TokenResponse:
    """模式 B：App 后端用自己的用户 ID + 共享密钥签名换取 Token。"""
    secret = runtime.settings.exchange_hmac_secret
    if not secret:
        raise forbidden("未启用 App 侧可信直传")

    now = unix_now()
    if abs(now - payload.timestamp) > runtime.settings.exchange_timestamp_tolerance_seconds:
        raise unauthorized("签名时间戳超出允许范围")

    expected = sign_hmac_hex(
        secret, f"{payload.external_user_id}.{payload.timestamp}"
    )
    if not constant_time_equals(expected, payload.signature.lower()):
        raise unauthorized("签名校验失败")

    result = await db.execute(
        select(User).where(User.external_user_id == payload.external_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            username=_username_for(payload.external_user_id),
            external_user_id=payload.external_user_id,
            display_name=payload.display_name or payload.external_user_id[:32],
            daily_quota=runtime.settings.default_daily_quota,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif not user.enabled:
        raise unauthorized("账号已被禁用")
    elif payload.display_name and payload.display_name != user.display_name:
        user.display_name = payload.display_name[:64]
        await db.commit()

    return await _issue_tokens(db, user, f"exchange:{payload.external_user_id}")


def _username_for(external_user_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", external_user_id)[:40] or "user"
    return f"ext_{safe}"


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(payload: RefreshRequest, db: DbSession) -> RefreshResponse:
    result = await db.execute(
        select(ApiClient).where(
            ApiClient.refresh_token_hash == hash_token(payload.refresh_token),
            ApiClient.revoked_at.is_(None),
        )
    )
    client = result.scalar_one_or_none()
    if client is None:
        raise unauthorized("refresh token 无效或已吊销")
    user = await db.get(User, client.user_id)
    if user is None or not user.enabled:
        raise unauthorized("账号已被禁用")
    client.last_used_at = utcnow()
    await db.commit()
    access_token, ttl = create_access_token(user.id, client.id)
    return RefreshResponse(access_token=access_token, expires_in=ttl)


@router.post("/logout", status_code=204)
async def logout(auth: CurrentAuth, db: DbSession) -> Response:
    auth.client.revoked_at = utcnow()
    await db.commit()
    return Response(status_code=204)
