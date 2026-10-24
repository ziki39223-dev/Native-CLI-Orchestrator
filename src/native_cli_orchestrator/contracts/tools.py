"""六个 MCP 工具的入参 / 出参契约。

冻结规则：工具名、字段名、错误码只增不改不删；Mock 与 Real 后端出参结构完全一致。

安全口径：出参**只出现账号标签（label）与序号**，绝不出现凭据、token、cookie。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .constants import (
    DEFAULT_TASK_TIMEOUT_S,
    GROUP_NAME_PATTERN,
    MAX_PROMPT_CHARS,
    MAX_TASK_TIMEOUT_S,
    AccountState,
    FaultKind,
    GroupKind,
    Mode,
    TaskState,
)
from .errors import ErrorCode, ToolErrorInfo


class _Strict(BaseModel):
    """公共配置：禁止多余字段，保证「契约即文档」。"""

    model_config = ConfigDict(extra="forbid")


#: 分组名约束类型。
#:
#: v1.1.0 之前分组是 Literal 三选一，pydantic 自动拦非法值；放开为任意字符串后，
#: 用 ``StringConstraints`` 保证"打错的名字"仍在入参校验阶段就被拦下（而不是当成新分组）。
GroupKey = Annotated[str, StringConstraints(pattern=GROUP_NAME_PATTERN)]


# --------------------------------------------------------------------------- #
# 公共信封
# --------------------------------------------------------------------------- #


class ToolEnvelope[DataT](_Strict):
    """所有工具统一出参信封。

    ``ok=False`` 时 ``data`` 恒为 ``null`` 且 ``error`` 有值。
    """

    ok: bool = True
    error_code: ErrorCode = ErrorCode.OK
    message: str = ""
    backend: Mode = Mode.MOCK
    correlation_id: str = ""
    elapsed_ms: int = 0
    error: ToolErrorInfo | None = None
    data: DataT | None = None


# --------------------------------------------------------------------------- #
# 任务清单（submit_task 的 task_manifest）
# --------------------------------------------------------------------------- #


class TaskManifest(_Strict):
    """任务清单，落盘为 ``manifest.json``。"""

    goal: str = Field(min_length=1, description="任务目标，人可读的一句话")
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS, description="要交给 CLI 的完整指令")
    acceptance: list[str] = Field(default_factory=list, description="验收标准，逐条可核对")
    version: str = Field(default="1.0", description="清单版本号，便于产物溯源")
    artifacts: list[str] = Field(
        default_factory=list,
        description="期望产物文件相对路径；为空则由 CLI 自行决定",
    )
    model: str = Field(default="", description="可选：指定模型名，空则用 CLI 默认")
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传元数据")

    @field_validator("goal", "prompt")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class TaskArtifact(_Strict):
    """产物文件描述。"""

    path: str = Field(description="相对任务工作目录的路径")
    size_bytes: int = 0
    sha256: str = ""


# --------------------------------------------------------------------------- #
# 1. submit_task
# --------------------------------------------------------------------------- #


class SubmitTaskRequest(_Strict):
    group: GroupKey
    task_manifest: TaskManifest
    timeout_s: float = Field(default=DEFAULT_TASK_TIMEOUT_S, gt=0, le=MAX_TASK_TIMEOUT_S)
    #: 是否等任务跑完再返回。False 时立刻返回 task_id，由 get_task_result 轮询。
    wait: bool = Field(default=False, description="False=异步提交，立即返回 task_id")


class SubmitTaskData(_Strict):
    task_id: str
    group: GroupKey
    state: TaskState
    #: 实际承接任务的账号序号（0 起）；异步提交时为预定账号。
    account_index: int = -1
    account_label: str = ""
    workspace: str = Field(description="任务工作目录绝对路径，含 manifest.json")
    submitted_at: datetime
    #: wait=True 时的等待摘要；异步提交为 False。
    waited: bool = False


# --------------------------------------------------------------------------- #
# 2. get_task_result
# --------------------------------------------------------------------------- #


class GetTaskResultRequest(_Strict):
    task_id: str = Field(min_length=1)
    #: 可选：最多等待多少秒等任务收尾（0 表示立刻返回当前状态）。
    wait_s: float = Field(default=0.0, ge=0, le=MAX_TASK_TIMEOUT_S)


class GetTaskResultData(_Strict):
    task_id: str
    group: GroupKey
    state: TaskState
    succeeded: bool
    account_index: int = -1
    account_label: str = ""
    summary: str = ""
    artifacts: list[TaskArtifact] = Field(default_factory=list)
    #: 失败原因摘要（state=failed 时有值），不含敏感信息。
    fail_reason: str = ""
    #: 识别出的故障类型，仅失败时有意义。
    fault_kind: str = ""
    #: 环境级错误码（CLI_NOT_FOUND / DEPENDENCY_MISSING / CONFIG_INVALID）；
    #: 非空表示任务没真正跑起来，编排侧应走告警而不是重试。
    error_code: str = ""
    exit_code: int | None = None
    elapsed_s: float = 0.0
    #: 本任务用掉的账号尝试次数（>1 表示发生过切换）。
    account_attempts: int = 0
    updated_at: datetime | None = None
    #: API 分组专用：token 用量与估算花费（CLI 分组恒为 0）。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_yuan: float = 0.0
    #: API 分组专用：是否因 max_output_tokens 被截断。
    truncated: bool = False


# --------------------------------------------------------------------------- #
# 3. get_pool_status
# --------------------------------------------------------------------------- #


class GetPoolStatusRequest(_Strict):
    #: 可选：只看某个分组。
    group: GroupKey | None = None


class AccountStatus(_Strict):
    index: int = Field(description="组内序号，0 起，越小越优先")
    label: str = Field(description="账号标签（非凭据），如 acct-1、备用A")
    state: AccountState
    #: 是否为本组当前唯一在线的账号。
    active: bool = False
    #: 是否正持有任务。
    busy: bool = False
    last_fault: FaultKind | None = None
    last_fault_at: datetime | None = None
    last_used_at: datetime | None = None
    #: 连续失败次数，达到阈值后自动转为 FAILED。
    consecutive_failures: int = 0
    notes: str = ""


class GroupStatus(_Strict):
    group: GroupKey
    #: 分组类型：cli（子进程+免费额度）/ api（HTTP+充值）。
    kind: GroupKind = GroupKind.CLI
    #: 当前激活账号序号；-1 表示全组无可用账号。
    active_index: int = -1
    #: 队列中等待的任务数。
    queued: int = 0
    running: int = 0
    accounts: list[AccountStatus] = Field(default_factory=list)
    #: API 分组专用：当日花费与预算（CLI 分组为空字典）。
    api_budget: dict[str, Any] = Field(default_factory=dict)


class GetPoolStatusData(_Strict):
    groups: list[GroupStatus] = Field(default_factory=list)
    #: 便于 DSH 一眼判断是否需要人工介入。
    healthy: bool = True
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 4. switch_account
# --------------------------------------------------------------------------- #


class SwitchAccountRequest(_Strict):
    group: GroupKey
    #: 可选：指定切到哪个序号；为空则按顺序切到"下一个可用"账号。
    target_index: int | None = Field(default=None, ge=0)
    reason: str = Field(default="manual", description="切换原因，仅记录用")


class SwitchAccountData(_Strict):
    group: GroupKey
    previous_index: int = -1
    active_index: int = -1
    active_label: str = ""
    #: 被切走的账号被标记成的状态（手动切换默认 DISABLED）。
    previous_state: AccountState | None = None
    switched: bool = False
    notes: str = ""


# --------------------------------------------------------------------------- #
# 5. reload_config
# --------------------------------------------------------------------------- #


class ReloadConfigRequest(_Strict):
    path: str | None = Field(default=None, description="账号池配置文件路径；空则用默认")


class ReloadConfigData(_Strict):
    loaded: bool
    path: str
    groups: list[str] = Field(default_factory=list)
    accounts_total: int = 0
    config_version: str = ""


# --------------------------------------------------------------------------- #
# 6. update_upstream
# --------------------------------------------------------------------------- #


class UpdateUpstreamRequest(_Strict):
    #: 只检查是否有新版本，不执行更新。
    check_only: bool = Field(default=False)
    #: 强制更新（忽略版本比较）。
    force: bool = Field(default=False)


class UpdateUpstreamData(_Strict):
    updated: bool = False
    from_version: str = ""
    to_version: str = ""
    #: 备份目录；未发生实际更新时为空。
    backup_path: str = ""
    changed_files: list[str] = Field(default_factory=list)
    #: 兼容性校验是否通过。
    compatible: bool = True
    #: 是否已回滚（更新失败时）。
    rolled_back: bool = False
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 便捷构造
# --------------------------------------------------------------------------- #


def success_envelope[DataT](
    data: DataT,
    *,
    backend: Mode,
    correlation_id: str,
    elapsed_ms: int,
    message: str = "",
) -> ToolEnvelope[DataT]:
    return ToolEnvelope[DataT](
        ok=True,
        error_code=ErrorCode.OK,
        message=message,
        backend=backend,
        correlation_id=correlation_id,
        elapsed_ms=elapsed_ms,
        error=None,
        data=data,
    )


def failure_envelope(
    error: ToolErrorInfo,
    *,
    backend: Mode,
    correlation_id: str,
    elapsed_ms: int,
) -> ToolEnvelope[Any]:
    return ToolEnvelope[Any](
        ok=False,
        error_code=error.code,
        message=error.message,
        backend=backend,
        correlation_id=correlation_id,
        elapsed_ms=elapsed_ms,
        error=error,
        data=None,
    )
