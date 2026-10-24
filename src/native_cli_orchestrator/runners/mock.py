"""Mock 执行器：开发机用，不拉起任何真实 CLI。

作用：让**调度逻辑本身**（选账号 → 失败 → 标记故障 → 换账号 → 重试）能在开发机上被完整测试，
而不需要安装 Trae / Qoder / WorkBuddy。

行为完全走真实协议：写 request.json、必要时写 .FAILED、写产物、写 .DONE，
因此 ``get_task_result`` / ``get_pool_status`` 的返回与真机路径一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..common.text import now_local, preview
from ..contracts.constants import DEFAULT_CLI_COMMANDS, FaultKind
from ..contracts.tools import TaskArtifact
from ..observability.faults import detect_fault
from ..observability.logging import get_logger
from ..tasks.workspace import TaskWorkspace
from .base import RunContext, Runner, RunResult

_log = get_logger("dao.cli")

#: Mock 场景名 → 故障类型（None 表示成功）。可在 config.yaml 里配置。
SCENARIOS: dict[str, FaultKind | None] = {
    "success": None,
    "quota": FaultKind.QUOTA_EXHAUSTED,
    "banned": FaultKind.BANNED,
    "auth": FaultKind.AUTH_INVALID,
    "rate_limit": FaultKind.RATE_LIMITED,
    "timeout": FaultKind.TIMEOUT,
    "crash": FaultKind.CRASHED,
}

#: 配额/封禁类故障对应的模拟 CLI 输出（同时用于验证故障识别正则）。
FAULT_MESSAGES: dict[FaultKind, str] = {
    FaultKind.QUOTA_EXHAUSTED: "Error: You have exceeded your current quota. usage limit reached.",
    FaultKind.BANNED: "Error: account is suspended due to policy violation.",
    FaultKind.AUTH_INVALID: "Error: 401 Unauthorized — token expired, please login again.",
    FaultKind.RATE_LIMITED: "Error: 429 Too Many Requests — rate limit exceeded.",
    FaultKind.TIMEOUT: "Error: request timed out after 300s.",
    FaultKind.CRASHED: "Traceback (most recent call last):\nRuntimeError: unexpected error",
}


@dataclass
class MockRunner(Runner):
    """按场景脚本模拟 CLI 行为。"""

    #: Mock 不校验凭据：开发机无需任何环境变量即可闭环调试。
    requires_credentials: bool = False

    #: 分组 → 账号序号 → 场景名。未配置的账号默认成功。
    plan: dict[str, dict[int, str]] = field(default_factory=dict)
    #: 模拟生成耗时（秒）。
    latency_s: float = 0.0
    #: 是否真的产出示例产物文件（便于验证产物收集）。
    emit_artifacts: bool = True

    #: 记录每次调用的 (group, account_index, attempt)，供断言使用。
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def run(self, context: RunContext) -> RunResult:
        self.calls.append((context.group, context.account_index, context.attempt))
        started = time.monotonic()
        workspace = context.workspace

        # 协议步骤一：写 inbox 请求。
        workspace.write_request(
            context.manifest,
            account_label=context.account_label,
            attempt=context.attempt,
        )

        if self.latency_s:
            time.sleep(min(self.latency_s, 0.2))

        scenario = self.plan.get(context.group, {}).get(context.account_index, "success")
        fault = SCENARIOS.get(scenario)
        if scenario not in SCENARIOS:
            # 未知场景名按配置错误处理，避免静默当成功。
            fault = FaultKind.CRASHED
            message = f"[mock] 未知场景：{scenario}"
        else:
            message = FAULT_MESSAGES.get(fault, "") if fault else ""

        if message:
            workspace.append_output(stderr=f"[mock] {message}\n")
            # 用真实识别器再过一遍，确保正则与场景一致。
            detected = detect_fault(message, exit_code=1)
            fault = detected or fault
            workspace.mark_failed(reason=f"{fault.value}: {message}" if fault else message)
            elapsed = time.monotonic() - started
            _log.info(
                "mock task=%s group=%s account=%s attempt=%d → 故障 %s",
                context.task_id,
                context.group,
                context.account_label,
                context.attempt,
                fault.value if fault else "unknown",
            )
            return RunResult(
                succeeded=False,
                fault=fault,
                exit_code=1,
                reason=message,
                elapsed_s=elapsed,
                retry_with_other_account=fault is not FaultKind.CRASHED,
            )

        return self._succeed(context, started)

    def _succeed(self, context: RunContext, started: float) -> RunResult:
        workspace = context.workspace
        manifest = context.manifest
        task_id = context.task_id
        account_label = context.account_label

        # 产出示例产物（验证产物收集与 sha256）。
        if self.emit_artifacts:
            _write_artifacts(workspace, task_id=task_id, group=context.group, label=account_label)

        summary = (
            f"[mock] {context.group} 第 {context.attempt} 次尝试成功"
            f"（账号 {account_label}）：{preview(manifest.goal)}"
        )
        workspace.append_output(stdout=f"[mock] 完成：{summary}\n")
        workspace.write_result(
            {
                "task_id": task_id,
                "group": context.group,
                "account_label": account_label,
                "succeeded": True,
                "summary": summary,
                "finished_at": now_local().isoformat(),
            }
        )
        workspace.mark_done(summary=summary)
        elapsed = time.monotonic() - started
        _log.info(
            "mock task=%s group=%s account=%s attempt=%d → 成功（%.0fms）",
            task_id,
            context.group,
            account_label,
            context.attempt,
            elapsed * 1000,
        )
        return RunResult(succeeded=True, exit_code=0, summary=summary, elapsed_s=elapsed)

    def shutdown(self) -> None:
        return None

    # ------------------------------------------------------------------ 便捷

    def available_commands(self) -> dict[str, list[str]]:
        """Mock 下 CLI 命令只是展示用，不做可执行性检查。"""
        return {group: list(cmd) for group, cmd in DEFAULT_CLI_COMMANDS.items()}


def _write_artifacts(
    workspace: TaskWorkspace, *, task_id: str, group: str, label: str
) -> list[TaskArtifact]:
    """写两个示例产物：一个在 artifacts/，一个在任务根（验证两处都收集）。"""
    manifest = workspace.read_manifest() if workspace.manifest_path.is_file() else None
    goal = manifest.goal if manifest else task_id

    primary = workspace.artifact_dir / "report.md"
    primary.write_text(
        f"# Mock 产物\n\n"
        f"- task_id: {task_id}\n"
        f"- group: {group}\n"
        f"- account: {label}\n"
        f"- goal: {goal}\n\n"
        f"这是 Mock 模式生成的占位产物，用于验证产物收集与验收流程。\n",
        encoding="utf-8",
    )

    extra = workspace.root / "outline.md"
    extra.write_text(f"# 大纲（Mock）\n\n1. 目标：{goal}\n2. 方法\n3. 结论\n", encoding="utf-8")

    return [
        TaskArtifact(path=primary.relative_to(workspace.root).as_posix()),
        TaskArtifact(path=extra.relative_to(workspace.root).as_posix()),
    ]


__all__ = ["FAULT_MESSAGES", "SCENARIOS", "MockRunner"]
