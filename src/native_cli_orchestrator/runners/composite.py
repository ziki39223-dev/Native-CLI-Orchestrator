"""复合执行器：按分组 kind 在 CLI 与 API 之间分派。

为什么需要它：CLI 组与 API 组要能**同时存在**（三家免费额度 + 自己充值的 API 并用）。
调度器只持有 ``Runner`` 接口，不知道背后是进程还是 HTTP。

分包规则：
- `kind=cli` → ``CliRunner``（子进程 + 文件协议）
- `kind=api` → ``ApiRunner``（OpenAI 兼容 HTTP）

没有命中时按 CLI 处理（历史行为），并由 ``CliRunner`` 自己报 CONFIG_INVALID。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.constants import GroupKind
from ..contracts.errors import ErrorCode, ToolError
from .base import RunContext, Runner, RunResult


@dataclass
class CompositeRunner(Runner):
    """把两个执行器按分组类型分派。"""

    cli: Runner
    api: Runner
    #: 分组名 → kind。由调度器在构造时灌入（见 ``bind_groups``）。
    kinds: dict[str, GroupKind] | None = None

    @property
    def requires_credentials(self) -> bool:  # type: ignore[override]
        """任一子执行器需要凭据就需要（API 组必须有 key）。"""
        return self.cli.requires_credentials or self.api.requires_credentials

    def bind_groups(self, kinds: dict[str, GroupKind]) -> None:
        """注入分组类型表；调度器构造后立即调用。"""
        self.kinds = dict(kinds)

    def _kind_for(self, group: str) -> GroupKind:
        if self.kinds is None:
            # 未注入时按 CLI 处理，保持旧行为（也让 CliRunner 报出明确错误）。
            return GroupKind.CLI
        kind = self.kinds.get(group)
        if kind is None:
            raise ToolError(
                ErrorCode.GROUP_NOT_FOUND,
                f"未配置分组：{group}",
                detail={"group": group, "available": sorted(self.kinds)},
            )
        return kind

    def run(self, context: RunContext) -> RunResult:
        kind = self._kind_for(context.group)
        if kind is GroupKind.API:
            return self.api.run(context)
        return self.cli.run(context)

    def budget_summary(self, group: str, daily_budget: float) -> dict[str, object]:
        """把预算摘要代理给 API 执行器（调度器只认这个接口）。"""
        summary_fn = getattr(self.api, "budget_summary", None)
        if not callable(summary_fn):
            return {}
        result: dict[str, object] = summary_fn(group, daily_budget)
        return result

    def shutdown(self) -> None:
        self.cli.shutdown()
        self.api.shutdown()


__all__ = ["CompositeRunner"]
