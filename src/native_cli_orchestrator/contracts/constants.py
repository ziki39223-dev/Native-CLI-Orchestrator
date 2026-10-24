"""全局常量与枚举：分组、账号状态、任务状态、故障类型。

这些值是契约的一部分（出参里直接出现），只增不改不删。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

#: 内置的三家 CLI 分组（`kind: cli`）。分组本身不再受枚举限制：
#: ``accounts.yml`` 里可以声明任意分组名，只需标注 ``kind``。
BUILTIN_CLI_GROUPS: Final[tuple[str, ...]] = ("trae_group", "qoder_group", "workbuddy_group")

#: 兼容旧名字：历史上分组就是这三个。
GROUPS: Final[tuple[str, ...]] = BUILTIN_CLI_GROUPS

#: 分组名类型。
#:
#: **v1.1.0 变更**：分组从「三选一枚举」放开为任意字符串，以便挂接任意家
#: OpenAI 兼容 API（DeepSeek / Kimi / GLM / OpenRouter / 本地 vLLM 等）。
#: 代价是工具层不再能在入参校验阶段拦掉未知分组，改为调度层返回
#: ``GROUP_NOT_FOUND``（见 docs/06 错误码表）——语义不变，只是发现得晚一点。
type GroupName = str

#: 分组名允许的字符集（字母/数字/下划线/连字符），用于校验。
GROUP_NAME_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"

#: 分组到 CLI 默认命令的映射（工作机可在 accounts.yml 里覆盖）。
DEFAULT_CLI_COMMANDS: Final[dict[str, tuple[str, ...]]] = {
    "trae_group": ("traecli", "mcp-server"),
    "qoder_group": ("qoder", "mcp", "serve"),
    "workbuddy_group": ("workbuddy-mcp",),
}

#: API 分组的默认 base_url（OpenAI 官方；其它家由配置覆盖）。
DEFAULT_API_BASE_URL: Final[str] = "https://api.openai.com/v1"
#: API 分组默认模型名（占位，必须由配置覆盖）。
DEFAULT_API_MODEL: Final[str] = "gpt-4o-mini"
#: API 调用默认最大输出 token。
DEFAULT_API_MAX_OUTPUT_TOKENS: Final[int] = 4096
#: API 分组默认日预算（人民币元）。0 表示不限额。
DEFAULT_API_DAILY_BUDGET: Final[float] = 0.0

#: 任务完成标记文件。
DONE_MARKER: Final[str] = ".DONE"
#: 任务失败标记文件（与 .DONE 互斥，谁先出现算谁）。
FAIL_MARKER: Final[str] = ".FAILED"
#: 任务清单文件名。
MANIFEST_NAME: Final[str] = "manifest.json"
#: 调度器写回的结果文件名。
RESULT_NAME: Final[str] = "result.json"
#: 调度器内部状态文件名。
STATE_NAME: Final[str] = ".task.json"

#: 提交任务时允许的最大内联 prompt 字符数。
MAX_PROMPT_CHARS: Final[int] = 200_000
#: 单任务默认超时秒数。
DEFAULT_TASK_TIMEOUT_S: Final[float] = 900.0
#: 单任务允许的最大超时秒数。
MAX_TASK_TIMEOUT_S: Final[float] = 7200.0
#: 同一任务最多尝试的账号数（含首个），超出则任务判失败。
DEFAULT_MAX_ACCOUNT_ATTEMPTS: Final[int] = 3


class Mode(StrEnum):
    """运行模式，同时是出参里的 ``backend`` 字段。"""

    MOCK = "mock"
    REAL = "real"


class GroupKind(StrEnum):
    """分组类型：决定这个分组怎么"干活"。

    ``CLI`` 走子进程 + 文件协议（三家客户端，免费额度）；
    ``API`` 走 OpenAI 兼容 HTTP 调用（DSH 自己充值，或任何中转/本地模型）。
    """

    CLI = "cli"
    API = "api"


class ApiProtocol(StrEnum):
    """API 分组的协议类型。一期只实现 OpenAI 兼容（覆盖面最大）。"""

    OPENAI = "openai"


class TransportKind(StrEnum):
    SSE = "sse"
    STDIO = "stdio"


class AccountState(StrEnum):
    """账号状态机。

    ``DORMANT`` 是默认态：进程不拉起、不登录、不请求，只在轮到时才激活；
    这是"同组只有 1 个账号在线"的落地方式。
    """

    #: 备用，未拉起任何进程。
    DORMANT = "dormant"
    #: 已选择为本组当前账号，但尚未执行任务。
    ACTIVE = "active"
    #: 正在执行任务。
    WORKING = "working"
    #: 故障（额度耗尽 / 封禁 / 连续超时），需人工恢复。
    FAILED = "failed"
    #: 手动下线，调度器不会选中。
    DISABLED = "disabled"


#: 视为"不可选"的账号状态。
UNAVAILABLE_STATES: Final[frozenset[AccountState]] = frozenset(
    {AccountState.FAILED, AccountState.DISABLED}
)


class TaskState(StrEnum):
    """任务状态。

    ``QUEUED`` / ``RUNNING`` 属于正常状态，``ok=true``；
    只有 ``FAILED`` 表示任务真的失败。
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FaultKind(StrEnum):
    """从 CLI 输出 / API 响应中识别出的故障类型，决定是否切换账号。"""

    #: 配额/额度耗尽。
    QUOTA_EXHAUSTED = "quota_exhausted"
    #: 账号被封禁或风控拦截。
    BANNED = "banned"
    #: 鉴权失效（token 过期 / 未登录）。
    AUTH_INVALID = "auth_invalid"
    #: 触发限流，短时间不可重试。
    RATE_LIMITED = "rate_limited"
    #: 超时。
    TIMEOUT = "timeout"
    #: 进程异常退出（非配额类）。
    CRASHED = "crashed"
    #: API 专用：上下文超长（不该换账号，应缩短 prompt）。
    CONTEXT_TOO_LONG = "context_too_long"
    #: API 专用：模型名不存在或无权限（不该换账号，应改配置）。
    MODEL_UNAVAILABLE = "model_unavailable"
    #: 未识别。


UNKNOWN_FAULT = "unknown"


#: 需要切换账号的故障（其余故障会判任务失败但不切换）。
#: 注意：CONTEXT_TOO_LONG / MODEL_UNAVAILABLE 是配置/prompt 问题，换账号没用。
SWITCHABLE_FAULTS: Final[frozenset[FaultKind]] = frozenset(
    {
        FaultKind.QUOTA_EXHAUSTED,
        FaultKind.BANNED,
        FaultKind.AUTH_INVALID,
        FaultKind.RATE_LIMITED,
        FaultKind.TIMEOUT,
    }
)
