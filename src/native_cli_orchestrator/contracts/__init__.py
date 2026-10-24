"""契约层：六个 MCP 工具的入参/出参、错误码、全局常量。

约束：只依赖标准库与 pydantic；不得 import 本项目其它层。
"""

from __future__ import annotations

from .constants import (
    DEFAULT_MAX_ACCOUNT_ATTEMPTS,
    DEFAULT_TASK_TIMEOUT_S,
    DONE_MARKER,
    GROUPS,
    MAX_PROMPT_CHARS,
    MAX_TASK_TIMEOUT_S,
    AccountState,
    FaultKind,
    GroupName,
    Mode,
    TaskState,
    TransportKind,
)
from .errors import ErrorCode, ToolError, ToolErrorInfo
from .tools import (
    AccountStatus,
    GetPoolStatusData,
    GetPoolStatusRequest,
    GetTaskResultData,
    GetTaskResultRequest,
    GroupStatus,
    ReloadConfigData,
    ReloadConfigRequest,
    SubmitTaskData,
    SubmitTaskRequest,
    SwitchAccountData,
    SwitchAccountRequest,
    TaskArtifact,
    ToolEnvelope,
    UpdateUpstreamData,
    UpdateUpstreamRequest,
)

__all__ = [
    "DEFAULT_MAX_ACCOUNT_ATTEMPTS",
    "DEFAULT_TASK_TIMEOUT_S",
    "DONE_MARKER",
    "GROUPS",
    "MAX_PROMPT_CHARS",
    "MAX_TASK_TIMEOUT_S",
    "AccountState",
    "AccountStatus",
    "ErrorCode",
    "FaultKind",
    "GetPoolStatusData",
    "GetPoolStatusRequest",
    "GetTaskResultData",
    "GetTaskResultRequest",
    "GroupName",
    "GroupStatus",
    "Mode",
    "ReloadConfigData",
    "ReloadConfigRequest",
    "SubmitTaskData",
    "SubmitTaskRequest",
    "SwitchAccountData",
    "SwitchAccountRequest",
    "TaskArtifact",
    "TaskState",
    "ToolEnvelope",
    "ToolError",
    "ToolErrorInfo",
    "TransportKind",
    "UpdateUpstreamData",
    "UpdateUpstreamRequest",
]
