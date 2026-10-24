"""错误码与结构化错误。

规则：
- 失败一律转成 ``ToolEnvelope(ok=False, error_code=...)``，不向 MCP 层抛裸异常；
- ``detail`` 只放排查信息，不含账号凭据、不含完整 prompt 正文；
- 错误码集合冻结，只增不改。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """工具错误码。一次失败只对应一个码。"""

    OK = "OK"
    #: 入参校验失败（缺字段、类型错、分组不存在、超长）。
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    #: 分组不存在或未配置账号。
    GROUP_NOT_FOUND = "GROUP_NOT_FOUND"
    #: 任务 ID 不存在（含已被回收）。
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    #: 该分组全部账号不可用（全部 FAILED/DISABLED 或全部额度耗尽）。
    POOL_EXHAUSTED = "POOL_EXHAUSTED"
    #: 账号凭据/配置缺失，无法拉起进程。
    ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
    #: CLI 可执行文件找不到。
    CLI_NOT_FOUND = "CLI_NOT_FOUND"
    #: 拉起/保活子进程失败。
    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    #: 任务超时（超过 timeout_s 仍未产出 .DONE）。
    TASK_TIMEOUT = "TASK_TIMEOUT"
    #: 任务失败（CLI 返回错误且不可通过换账号解决）。
    TASK_FAILED = "TASK_FAILED"
    #: 配置或规则文件校验失败。
    CONFIG_INVALID = "CONFIG_INVALID"
    #: 上游更新失败（已回滚）。
    UPSTREAM_UPDATE_FAILED = "UPSTREAM_UPDATE_FAILED"
    #: 上游更新后兼容性校验不通过（已回滚）。
    UPSTREAM_INCOMPATIBLE = "UPSTREAM_INCOMPATIBLE"
    #: real 模式缺少依赖或平台不支持。
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    #: 未分类内部错误兜底。
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolErrorInfo(BaseModel):
    """失败明细。"""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = ""
    #: 结构化上下文：分组、账号序号（**不含账号名/凭据**）、任务 ID、进程退出码等。
    detail: dict[str, Any] = Field(default_factory=dict)


class ToolError(Exception):
    """受控错误，由工具层捕获并映射成出参错误码。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_info(self) -> ToolErrorInfo:
        return ToolErrorInfo(code=self.code, message=self.message, detail=dict(self.detail))

    def __repr__(self) -> str:  # pragma: no cover
        return f"ToolError(code={self.code.value!r}, message={self.message!r})"
