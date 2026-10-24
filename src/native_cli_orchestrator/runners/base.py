"""Runner 接口：把"如何执行一个任务"抽象成可替换实现。

- ``MockRunner``：开发机用，不拉起任何进程，按脚本模拟成功/失败/额度耗尽；
- ``CliRunner``：工作机用，真实拉起 CLI 子进程（MCP over stdio），监控 .DONE；
- ``ApiRunner``：走 OpenAI 兼容 HTTP（DSH 自己充值，或任意中转/本地模型）。

三者接口一致，调度器只依赖本接口，因此所有后端的**调度逻辑完全同一条代码路径**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config.accounts import ApiSpec
from ..contracts.constants import FaultKind
from ..contracts.tools import TaskManifest
from ..tasks.workspace import TaskWorkspace


@dataclass(slots=True)
class RunContext:
    """一次任务执行所需的全部上下文。"""

    task_id: str
    group: str
    manifest: TaskManifest
    workspace: TaskWorkspace
    account_index: int
    account_label: str
    #: 账号对应的环境变量（已解析成实际值，**只在内存里**）。
    env: dict[str, str] = field(default_factory=dict)
    #: 本次是该任务的第几次尝试（1 起）。
    attempt: int = 1
    #: 单次尝试的超时秒数。
    timeout_s: float = 900.0
    #: API 分组专用：供应商参数（CLI 分组为 None）。
    api: ApiSpec | None = None
    #: API 分组专用：该账号已解析的 API key（**只在内存里**，绝不落盘/回显）。
    api_key: str = ""


@dataclass(slots=True)
class RunResult:
    """一次尝试的执行结果。"""

    succeeded: bool
    #: 故障类型；成功时为 None。
    fault: FaultKind | None = None
    exit_code: int | None = None
    #: 失败原因摘要（已脱敏）。
    reason: str = ""
    #: 结果摘要（成功时给 DSH 看的一段话）。
    summary: str = ""
    #: 本次尝试耗时。
    elapsed_s: float = 0.0
    #: 是否值得换账号重试（配额/封禁/限流/超时 → True；语法错误 → False）。
    retry_with_other_account: bool = False
    #: API 分组专用：本次用量（token），用于计量与预算。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: API 分组专用：按配置单价估算的花费（元）；未配价格时为 0。
    cost_yuan: float = 0.0
    #: API 分组专用：模型是否因 max_tokens 被截断。
    truncated: bool = False


class Runner(ABC):
    """任务执行器接口。"""

    #: 是否需要账号凭据。Mock 明确为 False：开发机没有环境变量也能跑通全流程，
    #: 否则"开发机无需真实凭据即可调试"这个核心需求就落不了地。
    requires_credentials: bool = True

    @abstractmethod
    def run(self, context: RunContext) -> RunResult:
        """执行一次任务尝试（阻塞直到 .DONE / .FAILED / 超时 / 进程退出）。"""

    def shutdown(self) -> None:
        """释放资源（终止仍在运行的子进程）。默认无操作。"""
        return


def resolve_cli_path(command: list[str]) -> Path | None:
    """在 PATH 里查找 CLI 可执行文件，找不到返回 None。"""
    import shutil

    if not command:
        return None
    found = shutil.which(command[0])
    return Path(found) if found else None


__all__ = ["RunContext", "RunResult", "Runner", "resolve_cli_path"]
