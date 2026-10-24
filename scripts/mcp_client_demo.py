"""命令行联调客户端：不装 Inspector 也能验证六个工具。

用法：
    python scripts/mcp_client_demo.py stdio
    python scripts/mcp_client_demo.py sse --url http://127.0.0.1:8770/sse
    python scripts/mcp_client_demo.py stdio --switch   # 额外验证账号切换

这是开发调试脚本，不属于 MCP 服务的运行依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

GROUPS = ("trae_group", "qoder_group", "workbuddy_group")


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
    return {}


def _dump(title: str, payload: dict[str, Any], *, limit: int = 1200) -> None:
    print(f"\n=== {title} ===")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:limit] + ("…" if len(text) > limit else ""))


async def _smoke(
    session: ClientSession, *, group: str, prompt: str, with_switch: bool
) -> int:
    failures = 0

    tools = await session.list_tools()
    _dump("工具清单", {"tools": sorted(tool.name for tool in tools.tools)})

    status = _payload(await session.call_tool("get_pool_status", {}))
    _dump("get_pool_status", status, limit=800)
    if not status.get("ok"):
        failures += 1

    submitted = _payload(
        await session.call_tool(
            "submit_task",
            {
                "group": group,
                "task_manifest": {
                    "goal": "联调：验证调度闭环",
                    "prompt": prompt,
                    "acceptance": ["有产物文件"],
                },
                "wait": False,
            },
        )
    )
    _dump("submit_task", submitted, limit=700)
    if not submitted.get("ok"):
        return failures + 1

    task_id = submitted["data"]["task_id"]
    print(f"\n[轮询] task_id={task_id}")

    result: dict[str, Any] = {}
    for attempt in range(1, 21):
        result = _payload(await session.call_tool("get_task_result", {"task_id": task_id, "wait_s": 2}))
        state = (result.get("data") or {}).get("state")
        print(f"  第 {attempt} 次 → state={state}")
        if state in {"succeeded", "failed", "cancelled"}:
            break
    _dump("get_task_result", result, limit=1500)

    data = result.get("data") or {}
    if data.get("state") != "succeeded":
        failures += 1
        print("[警告] 任务未成功，请检查故障注入配置或日志")
    else:
        print(f"[OK] 成功，尝试次数={data.get('account_attempts')} 产物={len(data.get('artifacts', []))}")

    if with_switch:
        switched = _payload(await session.call_tool("switch_account", {"group": group}))
        _dump("switch_account", switched, limit=500)
        if not switched.get("ok"):
            failures += 1

    return failures


async def run_stdio(group: str, prompt: str, with_switch: bool) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "native_cli_orchestrator.entry",
            "stdio",
            "--config",
            str(PROJECT_ROOT / "config" / "config.yaml"),
            "--log-dir",
            "",
        ],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await _smoke(session, group=group, prompt=prompt, with_switch=with_switch)


async def run_sse(url: str, group: str, prompt: str, with_switch: bool) -> int:
    async with sse_client(url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await _smoke(session, group=group, prompt=prompt, with_switch=with_switch)


def main() -> int:
    parser = argparse.ArgumentParser(description="Native-CLI-Orchestrator 联调客户端")
    parser.add_argument("transport", choices=["stdio", "sse"])
    parser.add_argument("--url", default="http://127.0.0.1:8770/sse")
    parser.add_argument("--group", default="trae_group", choices=list(GROUPS))
    parser.add_argument("--prompt", default="联调测试：请回复一句话")
    parser.add_argument("--switch", action="store_true", help="额外验证 switch_account")
    args = parser.parse_args()

    if args.transport == "stdio":
        failures = asyncio.run(run_stdio(args.group, args.prompt, args.switch))
    else:
        failures = asyncio.run(run_sse(args.url, args.group, args.prompt, args.switch))

    print("\n[结果]", "全部通过" if failures == 0 else f"{failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
