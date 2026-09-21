"""统一错误模型：所有非 2xx 响应都返回 {"error": {code, message, retry_after?}}。"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.headers = headers or {}

    def to_response(self) -> JSONResponse:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        headers = dict(self.headers)
        if self.retry_after is not None:
            headers.setdefault("Retry-After", str(self.retry_after))
        return JSONResponse(
            status_code=self.status_code,
            content={"error": payload},
            headers=headers,
        )


def invalid_input(message: str) -> AppError:
    return AppError(400, "INVALID_INPUT", message)


def unauthorized(message: str = "Token 缺失、过期或已吊销") -> AppError:
    return AppError(401, "UNAUTHORIZED", message)


def forbidden(message: str = "无权访问该任务") -> AppError:
    return AppError(403, "FORBIDDEN", message)


def not_found(message: str = "任务不存在或已过留存期") -> AppError:
    return AppError(404, "NOT_FOUND", message)


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return exc.to_response()


async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """把 FastAPI 的校验错误折叠成契约里的 INVALID_INPUT。"""
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    detail = first.get("msg", "参数不合法")
    message = f"{location}: {detail}" if location else detail
    return AppError(400, "INVALID_INPUT", message).to_response()
