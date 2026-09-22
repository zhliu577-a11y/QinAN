"""应用配置：全部通过环境变量注入，容器内由 compose 的 env_file 提供。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InstanceSpec:
    """一个 opencode 实例的连接信息。"""

    __slots__ = ("id", "host", "port", "password")

    def __init__(self, instance_id: str, host: str, port: int, password: str) -> None:
        self.id = instance_id
        self.host = host
        self.port = port
        self.password = password

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __repr__(self) -> str:  # 避免密码进日志
        return f"InstanceSpec({self.id!r}, {self.base_url!r})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("deploy/.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ===== 对外服务 =====
    public_base_url: str = "http://127.0.0.1:8080"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    mock_mode: bool = True

    # ===== 安全 =====
    jwt_secret: str = "dev-only-secret-change-me-at-least-32-bytes"
    access_token_ttl_seconds: int = 43200
    refresh_token_ttl_days: int = 30
    exchange_hmac_secret: str = ""
    exchange_timestamp_tolerance_seconds: int = 300
    callback_hmac_secret: str = ""
    callback_allowed_hosts: str = ""
    admin_token: str = ""
    sse_ping_interval_seconds: int = 15
    cors_allow_origins: str = ""

    # ===== 配额与队列 =====
    default_daily_quota: int = 30
    max_in_flight_per_user: int = 1
    max_queue_depth_per_user: int = 3
    task_timeout_seconds: int = 180
    max_text_chars: int = 200_000
    max_fetch_chars: int = 20_000
    max_output_chars: int = 4_000
    result_retention_days: int = 30
    quota_timezone_offset_hours: int = 8

    # ===== 实例池 =====
    pool_health_interval_seconds: int = 10
    pool_health_failure_threshold: int = 3
    instance_idle_dispose_minutes: int = 20
    dispatcher_interval_seconds: float = 0.5
    mock_step_delay_seconds: float = 0.6
    opencode_instances: str = ""
    opencode_server_username: str = "opencode"
    opencode_request_timeout_seconds: int = 30

    # ===== 模型 =====
    # 这里只决定「用哪个 provider 的哪个 model」；真正的服务地址与密钥在 opencode 实例侧，
    # 由 MODEL_BASE_URL / MODEL_API_KEY 注入（见 opencode/config/opencode.json）。
    # provider id 与 model id 必须真实存在，写错不会在启动时报错，而是等到第一次调用才失败。
    # 查询方式：https://models.dev/api.json，或进入容器执行 opencode models
    # 默认是 DeepSeek 官方的 flash 档：deepseek-flash（V4.1 Flash，1M 上下文）。
    # 同族的 deepseek-v4-flash（V4 Flash）规格与单价一样，可作备选。
    # 注意 models.dev 的清单不等于「你的账号/网关真能调」，最终以 GET {MODEL_BASE_URL}/models 为准。
    model_provider: str = "deepseek"
    model_name: str = "deepseek-flash"
    summarizer_agent: str = "summarizer"

    # ===== 存储 =====
    database_url: str = "sqlite:///./gateway.db"
    log_level: str = "INFO"

    # ===== 回调 =====
    callback_timeout_seconds: int = 5
    callback_max_attempts: int = 3
    callback_retry_delays: list[int] = Field(default_factory=lambda: [5, 30, 120])

    @property
    def instance_specs(self) -> list[InstanceSpec]:
        """解析 OPENCODE_INSTANCES，格式为 host:port:password，逗号分隔。

        密码本身可能含冒号，因此只切前两段。
        """
        specs: list[InstanceSpec] = []
        for index, raw in enumerate(self.opencode_instances.split(","), start=1):
            item = raw.strip()
            if not item:
                continue
            parts = item.split(":", 2)
            if len(parts) != 3:
                raise ValueError(
                    f"OPENCODE_INSTANCES 第 {index} 项格式非法，应为 host:port:password"
                )
            host, port_text, password = parts
            specs.append(
                InstanceSpec(
                    instance_id=f"oc-{index}",
                    host=host.strip(),
                    port=int(port_text.strip()),
                    password=password,
                )
            )
        return specs

    @property
    def callback_allowed_host_set(self) -> set[str]:
        return {
            host.strip().lower()
            for host in self.callback_allowed_hosts.split(",")
            if host.strip()
        }

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
