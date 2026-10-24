"""MCP Server 装配：注册六个工具、配置生命周期。

工具函数只做「原始入参 → pydantic 校验 → ToolService → dict」；
所有失败都通过信封返回，不让客户端看到裸异常。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from ..config.settings import AppSettings
from ..contracts.tools import (
    GetPoolStatusRequest,
    GetTaskResultRequest,
    ReloadConfigRequest,
    SubmitTaskRequest,
    SwitchAccountRequest,
    UpdateUpstreamRequest,
)
from ..observability.logging import configure_logging
from ..tools.service import ToolService, build_service

_log = logging.getLogger("dao.server")

SERVER_NAME = "native-cli-orchestrator"
SERVER_VERSION = "0.1.0"
SERVER_INSTRUCTIONS = (
    "三家 CLI 账号池的聚合调度服务：DSH 只需按分组提交任务，"
    "账号选择、额度耗尽切换、备用账号休眠都由本服务负责。\n"
    "典型流程：submit_task(wait=false) → 轮询 get_task_result 直到 state∈{succeeded,failed}。\n"
    "同组任务串行（一次只激活一个账号），跨组并行；"
    "get_pool_status 可随时查看账号在线/休眠/故障状态。"
)


def register_tools(server: MCPServer, service: ToolService) -> None:
    """把六个工具注册到 MCP Server。"""

    @server.tool(
        name="submit_task",
        description="提交任务到指定分组；调度器分配该组当前在线账号执行（默认异步，立即返回 task_id）",
    )
    def submit_task(
        group: str,
        task_manifest: dict[str, Any],
        timeout_s: float | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """提交任务。

        Args:
            group: 分组，取值 trae_group / qoder_group / workbuddy_group。
            task_manifest: 任务清单 {goal, prompt, acceptance?, version?, artifacts?, model?}。
            timeout_s: 单次尝试超时秒数；为空用配置默认值。
            wait: True 表示阻塞到任务结束再返回（长任务建议 False）。
        """
        try:
            request = SubmitTaskRequest(
                group=group,
                task_manifest=task_manifest,  # type: ignore[arg-type]
                **({"timeout_s": timeout_s} if timeout_s is not None else {}),
                wait=wait,
            )
        except ValidationError as exc:
            return service.invalid("submit_task", exc)
        return service.submit_task(request).model_dump(mode="json")

    @server.tool(
        name="get_task_result",
        description="获取任务状态与产物；state=queued/running 属正常，继续轮询即可",
    )
    def get_task_result(task_id: str, wait_s: float = 0.0) -> dict[str, Any]:
        """取任务结果。

        Args:
            task_id: submit_task 返回的任务 ID。
            wait_s: 最多等待多少秒让任务收尾（0=立刻返回当前状态）。
        """
        try:
            request = GetTaskResultRequest(task_id=task_id, wait_s=wait_s)
        except ValidationError as exc:
            return service.invalid("get_task_result", exc)
        return service.get_task_result(request).model_dump(mode="json")

    @server.tool(
        name="get_pool_status",
        description="查看所有分组的账号状态（在线/休眠/故障）与队列长度",
    )
    def get_pool_status(group: str | None = None) -> dict[str, Any]:
        """查看账号池状态。

        Args:
            group: 可选，只看某个分组。
        """
        try:
            request = GetPoolStatusRequest(group=group)
        except ValidationError as exc:
            return service.invalid("get_pool_status", exc)
        return service.get_pool_status(request).model_dump(mode="json")

    @server.tool(
        name="switch_account",
        description="手动把分组强制切到下一个（或指定的）备用账号，当前账号被下线",
    )
    def switch_account(
        group: str,
        target_index: int | None = None,
        reason: str = "manual",
    ) -> dict[str, Any]:
        """手动切换账号。

        Args:
            group: 分组名。
            target_index: 指定序号（0 起）；为空则切到下一个可用账号。
            reason: 记录用原因。
        """
        try:
            request = SwitchAccountRequest(
                group=group,
                target_index=target_index,
                reason=reason,
            )
        except ValidationError as exc:
            return service.invalid("switch_account", exc)
        return service.switch_account(request).model_dump(mode="json")

    @server.tool(
        name="reload_config",
        description="重载 accounts.yml 账号池配置（新增/下线账号、调整备用顺序），无需重启服务",
    )
    def reload_config(path: str | None = None) -> dict[str, Any]:
        """重载账号池配置。

        Args:
            path: 配置文件路径；为空则重载默认 config/accounts.yml。
        """
        try:
            request = ReloadConfigRequest(path=path)
        except ValidationError as exc:
            return service.invalid("reload_config", exc)
        return service.reload_config(request).model_dump(mode="json")

    @server.tool(
        name="update_upstream",
        description="手动触发上游同步（CLI 命令/接口 schema/模型清单），带备份与失败回滚",
    )
    def update_upstream(check_only: bool = False, force: bool = False) -> dict[str, Any]:
        """上游同步。

        Args:
            check_only: 只检查版本，不修改文件。
            force: 忽略版本比较强制执行。
        """
        try:
            request = UpdateUpstreamRequest(check_only=check_only, force=force)
        except ValidationError as exc:
            return service.invalid("update_upstream", exc)
        return service.update_upstream(request).model_dump(mode="json")


@asynccontextmanager
async def server_lifespan(_server: MCPServer) -> AsyncIterator[None]:
    """进程生命周期：启动/退出日志。"""
    _log.info("MCP Server 启动：%s", SERVER_NAME)
    try:
        yield
    finally:
        _log.info("MCP Server 退出：%s", SERVER_NAME)


def build_server(settings: AppSettings) -> MCPServer:
    """按配置构建可运行的 MCP Server（尚未启动传输）。"""
    configure_logging(
        level=settings.server.log_level,
        log_dir=settings.log_dir,
        force=True,
    )

    service = build_service(settings)
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=server_lifespan,
    )
    register_tools(server, service)
    # 供优雅退出时取回服务实例（MCPServer 不暴露自定义属性，这里挂私有字段）。
    server._nco_service = service  # type: ignore[attr-defined]
    _log.info(
        "装配完成：mode=%s transport=%s accounts=%s workspace=%s",
        settings.mode.value,
        settings.transport.value,
        settings.accounts_path,
        settings.workspace_root,
    )
    return server


def shutdown_server(server: MCPServer) -> None:
    """优雅退出：停止调度器（终止子进程、保存账号池状态）。"""
    service = getattr(server, "_nco_service", None)
    if isinstance(service, ToolService):
        service.shutdown()


__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "build_server", "register_tools", "shutdown_server"]
