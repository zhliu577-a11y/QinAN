"""口令哈希、JWT 签发与校验、HMAC 签名。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any, Optional

import bcrypt
import jwt

from .config import get_settings
from .timeutil import utcnow

_BCRYPT_MAX_BYTES = 72
ALGORITHM = "HS256"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _password_bytes(password: str) -> bytes:
    """bcrypt 只处理前 72 字节，超长直接报错，这里显式截断。"""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("ascii"))
    except ValueError:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def create_access_token(user_id: int, client_id: str) -> tuple[str, int]:
    settings = get_settings()
    ttl = settings.access_token_ttl_seconds
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "cid": client_id,
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, ttl


def decode_access_token(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def sign_hmac_hex(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_hmac_sha256_header(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
