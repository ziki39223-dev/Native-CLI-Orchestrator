"""工具服务：六个工具的统一入口。

职责：入参校验 → 调调度器/更新器 → 出参封装 → 错误映射 → 日志（入参摘要、耗时、错误码）。

纪律：
- 不出现进程管理、文件协议细节（都在 pools / runners / tasks 里）；
- 任何异常都转成 ``ToolEnvelope(ok=False)``；
- 日志与出参只出现账号 label，不出现凭据。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..common.ids import new_correlation_id
from ..common.text import preview
from ..config.accounts import AccountsConfig, load_accounts
from ..config.settings import AppSettings
from ..contracts.constants import Mode
from ..contracts.errors import ErrorCode, ToolError, ToolErrorInfo
from ..contracts.tools import (
    GetPoolStatusData,
    GetPoolStatusRequest,
    GetTaskResultData,
    GetTaskResultRequest,
    ReloadConfigData,
    ReloadConfigRequest,
    SubmitTaskData,
    SubmitTaskRequest,
    SwitchAccountData,
    SwitchAccountRequest,
    ToolEnvelope,
    UpdateUpstreamData,
    UpdateUpstreamRequest,
    failure_envelope,
    success_envelope,
)
from ..observability.logging import get_logger
from ..observability.timing import Stopwatch
from ..pools.scheduler import Scheduler
from ..runners.cli import CliRunner
from ..runners.composite import CompositeRunner
from ..runners.mock import MockRunner
from ..upstream.updater import UpstreamUpdater

_log = get_logger("dao.scheduler")


def build_runner(settings: AppSettings, accounts: AccountsConfig) -> CompositeRunner:
    """按 mode 构造执行器。

    real 模式下把 accounts.yml 里的自定义 ``cli`` 命令灌进 CliRunner 的画像表，
    因此三家 CLI 的命令差异完全由配置驱动，不需要改代码。

    此外把**所有** api 类型的分组装进 ApiRunner，由 ``CompositeRunner`` 按
    分组 kind 分派 —— 这样 CLI 组与 API 组可以同时存在、同时可跑。
    """
    if settings.is_mock:
        return CompositeRunner(
            cli=MockRunner(plan={}),
            api=MockRunner(plan={}),
        )

    from ..runners.api import ApiRunner
    from ..runners.cli import PROFILES, CliProfile

    profiles = dict(PROFILES)
    for name in accounts.cli_group_names:
        configured = accounts.group(name).command
        if configured:
            base = profiles.get(name) or CliProfile(group=name, command=tuple(configured))
            profiles[name] = CliProfile(
                group=name,
                command=tuple(configured),
                prompt_as_arg=base.prompt_as_arg,
                extra_args=base.extra_args,
                long_running=base.long_running,
                cwd_subdir=base.cwd_subdir,
            )

    return CompositeRunner(
        cli=CliRunner(profiles=profiles, output_tail_lines=settings.runner.output_tail_lines),
        api=ApiRunner(budget_path=settings.workspace_root / ".api_budget.json"),
    )


class ToolService:
    """六个工具的实现入口。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        accounts: AccountsConfig,
        scheduler: Scheduler,
        updater: UpstreamUpdater | None = None,
    ) -> None:
        self._settings = settings
        self._accounts = accounts
        self._scheduler = scheduler
        self._updater = updater

    # ------------------------------------------------------------------ 1. submit_task

    def submit_task(self, request: SubmitTaskRequest) -> ToolEnvelope[SubmitTaskData]:
        return self._run(
            "submit_task",
            {
                "group": request.group,
                "goal": preview(request.task_manifest.goal),
                "prompt_len": len(request.task_manifest.prompt),
                "acceptance": len(request.task_manifest.acceptance),
                "wait": request.wait,
                "timeout_s": request.timeout_s,
            },
            lambda: self._scheduler.submit(
                group=request.group,
                manifest=request.task_manifest,
                timeout_s=request.timeout_s,
                wait=request.wait,
            ),
        )

    # ------------------------------------------------------------------ 2. get_task_result

    def get_task_result(self, request: GetTaskResultRequest) -> ToolEnvelope[GetTaskResultData]:
        return self._run(
            "get_task_result",
            {"task_id": request.task_id, "wait_s": request.wait_s},
            lambda: self._scheduler.task_result(request.task_id, wait_s=request.wait_s),
        )

    # ------------------------------------------------------------------ 3. get_pool_status

    def get_pool_status(
        self, request: GetPoolStatusRequest | None = None
    ) -> ToolEnvelope[GetPoolStatusData]:
        req = request or GetPoolStatusRequest()
        return self._run(
            "get_pool_status",
            {"group": req.group},
            lambda: self._scheduler.pool_status(group=req.group),
        )

    # ------------------------------------------------------------------ 4. switch_account

    def switch_account(self, request: SwitchAccountRequest) -> ToolEnvelope[SwitchAccountData]:
        return self._run(
            "switch_account",
            {"group": request.group, "target_index": request.target_index, "reason": request.reason},
            lambda: self._scheduler.switch_account(
                group=request.group,
                target_index=request.target_index,
                reason=request.reason,
            ),
        )

    # ------------------------------------------------------------------ 5. reload_config

    def reload_config(self, request: ReloadConfigRequest | None = None) -> ToolEnvelope[ReloadConfigData]:
        req = request or ReloadConfigRequest()
        path = Path(req.path) if req.path else self._settings.accounts_path

        def _do() -> ReloadConfigData:
            accounts = load_accounts(path)
            self._accounts = accounts
            self._scheduler.reload_accounts(accounts)
            return ReloadConfigData(
                loaded=True,
                path=str(path),
                groups=accounts.group_names,
                accounts_total=accounts.account_count,
                config_version=accounts.config_version,
            )

        return self._run("reload_config", {"path": str(path)}, _do)

    # ------------------------------------------------------------------ 6. update_upstream

    def update_upstream(
        self, request: UpdateUpstreamRequest | None = None
    ) -> ToolEnvelope[UpdateUpstreamData]:
        req = request or UpdateUpstreamRequest()

        def _do() -> UpdateUpstreamData:
            if self._updater is None:
                raise ToolError(
                    ErrorCode.UPSTREAM_UPDATE_FAILED,
                    "上游更新能力未启用（config.yaml 里 upstream.enabled=false）",
                    detail={},
                )
            if not self._settings.upstream.enabled:
                raise ToolError(
                    ErrorCode.UPSTREAM_UPDATE_FAILED,
                    "上游更新能力未启用（config.yaml 里 upstream.enabled=false）",
                    detail={},
                )
            return self._updater.update(check_only=req.check_only, force=req.force)

        return self._run(
            "update_upstream",
            {"check_only": req.check_only, "force": req.force},
            _do,
        )

    # ------------------------------------------------------------------ 生命周期

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    def shutdown(self) -> None:
        """停止调度器（终止子进程、保存账号池状态）。"""
        self._scheduler.shutdown()

    # ------------------------------------------------------------------ 内部

    def invalid(self, tool_name: str, exc: Any) -> dict[str, Any]:
        """把 pydantic 校验错误转成契约信封 dict。"""
        fields = [
            {"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"]}
            for err in getattr(exc, "errors", list)()
        ]
        correlation_id = new_correlation_id()
        _log.warning(
            "tool=%s cid=%s → INVALID_ARGUMENT %s",
            tool_name,
            correlation_id,
            [field["loc"] for field in fields],
        )
        return failure_envelope(
            ToolErrorInfo(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"{tool_name} 入参校验失败",
                detail={"fields": fields},
            ),
            backend=self._settings.mode,
            correlation_id=correlation_id,
            elapsed_ms=0,
        ).model_dump(mode="json")

    def _run(self, tool_name: str, args_summary: dict[str, Any], action: Any) -> ToolEnvelope[Any]:
        correlation_id = new_correlation_id()
        backend: Mode = self._settings.mode

        with Stopwatch() as watch:
            try:
                data = action()
            except ToolError as exc:
                elapsed = watch.elapsed_ms
                _log.warning(
                    "tool=%s cid=%s backend=%s args=%s → %s (%dms) %s",
                    tool_name,
                    correlation_id,
                    backend.value,
                    args_summary,
                    exc.code.value,
                    elapsed,
                    exc.message,
                )
                return failure_envelope(
                    exc.to_info(),
                    backend=backend,
                    correlation_id=correlation_id,
                    elapsed_ms=elapsed,
                )
            except Exception as exc:  # noqa: BLE001 - 兜底：不允许裸异常冒到 MCP 层
                elapsed = watch.elapsed_ms
                _log.exception(
                    "tool=%s cid=%s backend=%s args=%s → INTERNAL_ERROR (%dms)",
                    tool_name,
                    correlation_id,
                    backend.value,
                    args_summary,
                    elapsed,
                )
                return failure_envelope(
                    ToolErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR,
                        message=f"未预期错误：{type(exc).__name__}",
                        detail={"reason": str(exc)[:300]},
                    ),
                    backend=backend,
                    correlation_id=correlation_id,
                    elapsed_ms=elapsed,
                )

        elapsed = watch.elapsed_ms
        _log.info(
            "tool=%s cid=%s backend=%s args=%s → OK (%dms)",
            tool_name,
            correlation_id,
            backend.value,
            args_summary,
            elapsed,
        )
        return success_envelope(
            data,
            backend=backend,
            correlation_id=correlation_id,
            elapsed_ms=elapsed,
        )


def build_service(settings: AppSettings) -> ToolService:
    """按配置装配工具服务（启动时调用一次）。"""
    accounts = load_accounts(settings.accounts_path)
    runner = build_runner(settings, accounts)

    state_path = settings.workspace_root / ".pools.state.json"
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=settings.workspace_root,
        state_path=state_path,
        max_account_attempts=settings.runner.max_account_attempts,
        default_timeout_s=settings.runner.default_timeout_s,
        group_cooldown_s=settings.runner.group_cooldown_s,
        switch_cooldown_s=settings.runner.switch_cooldown_s,
        failure_threshold=settings.runner.failure_threshold,
    )

    updater = None
    if settings.upstream.enabled:
        updater = UpstreamUpdater(
            settings=settings.upstream,
            project_root=settings.workspace_root.parent,
            backup_root=settings.backup_root,
        )

    return ToolService(
        settings=settings,
        accounts=accounts,
        scheduler=scheduler,
        updater=updater,
    )


__all__ = ["ToolService", "build_runner", "build_service", "logging"]
