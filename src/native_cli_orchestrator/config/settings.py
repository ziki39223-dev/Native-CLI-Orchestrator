"""``config.yaml`` 模型。

优先级（高 → 低）：命令行显式参数 > 环境变量 > ``config.local.yaml`` > ``config/config.yaml`` > 内置默认。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.constants import (
    DEFAULT_MAX_ACCOUNT_ATTEMPTS,
    DEFAULT_TASK_TIMEOUT_S,
    MAX_TASK_TIMEOUT_S,
    Mode,
    TransportKind,
)


class ServerSettings(BaseModel):
    """MCP 传输层参数。"""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="127.0.0.1", description="SSE 监听地址；局域网需显式改 0.0.0.0")
    port: int = Field(default=8770, ge=1, le=65535)
    sse_path: str = "/sse"
    message_path: str = "/messages/"
    #: 可选最小防护：非空时要求 ``Authorization: Bearer <token>``。
    auth_token: str = ""
    log_level: str = "INFO"


class RunnerSettings(BaseModel):
    """子进程与任务执行参数。"""

    model_config = ConfigDict(extra="forbid")

    #: 单任务默认超时。
    default_timeout_s: float = Field(default=DEFAULT_TASK_TIMEOUT_S, gt=0, le=MAX_TASK_TIMEOUT_S)
    #: 单任务最多尝试的账号数（含首个）。
    max_account_attempts: int = Field(default=DEFAULT_MAX_ACCOUNT_ATTEMPTS, ge=1, le=10)
    #: 轮询 .DONE 的间隔。
    poll_interval_s: float = Field(default=1.0, ge=0.05, le=30.0)
    #: 同一分组两次任务之间的最小间隔（风控限流，见需求阶段 5）。
    group_cooldown_s: float = Field(default=2.0, ge=0.0, le=600.0)
    #: 切换账号后额外的冷却秒数，避免"刚切换就高频请求"。
    switch_cooldown_s: float = Field(default=5.0, ge=0.0, le=600.0)
    #: 连续失败多少次后把账号标成 FAILED。
    failure_threshold: int = Field(default=2, ge=1, le=10)
    #: 子进程输出保留的最大行数（内存环形缓冲）。
    output_tail_lines: int = Field(default=500, ge=50, le=5000)


class UpstreamSettings(BaseModel):
    """上游同步参数。"""

    model_config = ConfigDict(extra="forbid")

    #: 是否启用上游更新能力（需求里属可选能力）。
    enabled: bool = True
    #: 上游仓库地址（git URL 或本地路径；开发机可指向测试仓库）。
    repo: str = ""
    #: 分支 / tag。
    ref: str = "main"
    #: 需要同步的子路径（相对仓库根），空表示整仓。
    paths: list[str] = Field(default_factory=list)
    #: 备份保留份数。
    keep_backups: int = Field(default=5, ge=1, le=50)
    #: 更新后是否自动跑兼容性校验。
    verify_after_update: bool = True


class AppSettings(BaseModel):
    """全局配置。"""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = Field(default=Mode.MOCK, description="mock / real")
    transport: TransportKind = TransportKind.SSE

    #: 账号池配置。
    accounts_path: Path = Field(default=Path("config/accounts.yml"))
    #: 任务工作目录根。
    workspace_root: Path = Field(default=Path("workspace"))
    #: 上游更新备份目录。
    backup_root: Path = Field(default=Path("backups"))
    log_dir: Path | None = Field(default=Path("logs"))

    server: ServerSettings = Field(default_factory=ServerSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)

    @property
    def is_mock(self) -> bool:
        return self.mode is Mode.MOCK
