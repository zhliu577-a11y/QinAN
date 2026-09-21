"""对外 API 的请求/响应模型，字段与 docs/API.md 一一对应。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    daily_quota: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str
    user: UserOut


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class TokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_id: Optional[str] = Field(default=None, max_length=128)


class ExchangeRequest(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=64)
    timestamp: int
    signature: str = Field(min_length=16, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=256)


class MeResponse(BaseModel):
    id: int
    username: str
    display_name: str
    daily_quota: int
    used_today: int
    remaining_today: int
    queue_depth: int
    in_flight_limit: int


class CreateTaskRequest(BaseModel):
    kind: Literal["url", "text"]
    url: Optional[str] = Field(default=None, max_length=2048)
    text: Optional[str] = Field(default=None)
    instruction: Optional[str] = Field(default=None, max_length=4000)
    max_output_chars: int = Field(default=1200, ge=100, le=4000)
    callback_url: Optional[str] = Field(default=None, max_length=512)
    client_task_id: Optional[str] = Field(default=None, max_length=64)


class TaskCreatedResponse(BaseModel):
    task_id: str
    status: str
    queue_pos: int
    estimated_wait_seconds: int
    created_at: str


class TaskUsage(BaseModel):
    tokens_in: int
    tokens_out: int
    duration_ms: int


class TaskSource(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    fetched_chars: Optional[int] = None


class TaskError(BaseModel):
    code: str
    message: str


class TaskResponse(BaseModel):
    task_id: str
    client_task_id: Optional[str] = None
    kind: str
    status: str
    queue_pos: int = 0
    result_md: Optional[str] = None
    source: Optional[TaskSource] = None
    usage: Optional[TaskUsage] = None
    error: Optional[TaskError] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TaskListItem(BaseModel):
    task_id: str
    status: str
    kind: str
    created_at: Optional[str] = None
    summary_head: Optional[str] = None


class TaskListResponse(BaseModel):
    items: list[TaskListItem]
    next_cursor: Optional[str] = None


class CancelResponse(BaseModel):
    task_id: str
    status: str


class HealthResponse(BaseModel):
    status: str
    engine: str
    pool_idle: int
    pool_total: int
    queue_len: int


class AdminCreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=64)
    daily_quota: Optional[int] = Field(default=None, ge=0, le=10000)


class AdminCreateUserResponse(BaseModel):
    id: int
    username: str
    display_name: str
    daily_quota: int


class AdminUpdateUserRequest(BaseModel):
    daily_quota: Optional[int] = Field(default=None, ge=0, le=10000)
    enabled: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=256)
