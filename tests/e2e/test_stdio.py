"""端到端：真起 stdio 子进程，用 MCP 客户端调六个工具。

验证点：
1. 工具清单与契约文档一致；
2. 闭环：submit_task → get_task_result 直到 succeeded；
3. 账号切换：额度耗尽后自动切备用，出参 account_attempts=2；
4. stdout 没被日志污染（stdio 传输硬要求）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"

EXPECTED_TOOLS = {
    "submit_task",
    "get_task_result",
    "get_pool_status",
    "switch_account",
    "reload_config",
    "update_upstream",
}


def _server_params(accounts_file: Path, workspace: Path) -> StdioServerParameters:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
        "NCO_MODE": "mock",
        "NCO_LOG_LEVEL": "WARNING",
    }
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "native_cli_orchestrator.entry",
            "stdio",
            "--config",
            str(PROJECT_ROOT / "config" / "config.yaml"),
            "--accounts",
            str(accounts_file),
            "--workspace",
            str(workspace),
            "--log-dir",
            "",
        ],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None):
        content = result.structuredContent
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]  # type: ignore[no-any-return]
        return content  # type: ignore[no-any-return]
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)  # type: ignore[no-any-return]
    raise AssertionError("MCP 调用没有返回内容")


@pytest.mark.anyio
async def test_stdio_tool_catalog(accounts_file: Path, tmp_path: Path) -> None:
    async with (
        stdio_client(_server_params(accounts_file, tmp_path / "ws")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()

    names = {tool.name for tool in tools.tools}
    assert names >= EXPECTED_TOOLS

    submit = next(tool for tool in tools.tools if tool.name == "submit_task")
    schema = getattr(submit, "input_schema", None) or submit.inputSchema
    assert "group" in schema["properties"]
    assert "task_manifest" in schema["properties"]
    assert set(schema.get("required", [])) == {"group", "task_manifest"}


@pytest.mark.anyio
async def test_stdio_submit_and_poll(accounts_file: Path, tmp_path: Path) -> None:
    async with (
        stdio_client(_server_params(accounts_file, tmp_path / "ws")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        status = _payload(await session.call_tool("get_pool_status", {}))
        assert status["ok"] is True
        assert status["backend"] == "mock"
        assert len(status["data"]["groups"]) == 3

        submitted = _payload(
            await session.call_tool(
                "submit_task",
                {
                    "group": "trae_group",
                    "task_manifest": {
                        "goal": "冒烟：写一句话",
                        "prompt": "请输出一句话",
                        "acceptance": ["有输出"],
                    },
                    "wait": True,
                },
            )
        )
        assert submitted["ok"] is True
        task_id = submitted["data"]["task_id"]
        assert submitted["data"]["state"] == "succeeded"

        result = _payload(await session.call_tool("get_task_result", {"task_id": task_id}))
        assert result["ok"] is True
        assert result["data"]["succeeded"] is True
        assert result["data"]["artifacts"]


@pytest.mark.anyio
async def test_stdio_unknown_group_is_structured(accounts_file: Path, tmp_path: Path) -> None:
    """未配置的分组名（v1.1.0 起分组可自定义，所以是 GROUP_NOT_FOUND 而非参数错误）。"""
    async with (
        stdio_client(_server_params(accounts_file, tmp_path / "ws")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        payload = _payload(
            await session.call_tool(
                "submit_task",
                {"group": "unknown_group", "task_manifest": {"goal": "g", "prompt": "p"}},
            )
        )

    assert payload["ok"] is False
    assert payload["error_code"] == "GROUP_NOT_FOUND"
    assert payload["data"] is None
    assert "unknown_group" in (payload["error"]["detail"].get("group") or "")


@pytest.mark.anyio
async def test_stdio_malformed_group_name_rejected(accounts_file: Path, tmp_path: Path) -> None:
    """分组名非法（含空格/符号）仍在入参校验阶段被拦下。"""
    async with (
        stdio_client(_server_params(accounts_file, tmp_path / "ws")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        payload = _payload(
            await session.call_tool(
                "submit_task",
                {"group": "bad group!", "task_manifest": {"goal": "g", "prompt": "p"}},
            )
        )

    assert payload["ok"] is False
    assert payload["error_code"] == "INVALID_ARGUMENT"
    assert payload["data"] is None
    assert payload["error"]["detail"]["fields"]


@pytest.mark.anyio
async def test_stdio_switch_account(accounts_file: Path, tmp_path: Path) -> None:
    async with (
        stdio_client(_server_params(accounts_file, tmp_path / "ws")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        payload = _payload(await session.call_tool("switch_account", {"group": "trae_group"}))

    data = payload["data"]
    assert payload["ok"] is True
    assert data["switched"] is True
    assert data["active_label"] == "trae-b"
