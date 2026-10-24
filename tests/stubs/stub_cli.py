"""桩 CLI：在开发机上真实模拟"三家 CLI"的子进程行为。

它不是 Mock 执行器——它是**真的被 subprocess.Popen 拉起来的独立进程**，
因此能验证 `CliRunner` 的真实链路：环境注入、输出泵、`.DONE` 检测、超时、进程树清理。

行为通过环境变量 ``STUB_MODE`` 控制：

| STUB_MODE | 行为 |
| --- | --- |
| `ok`（默认） | 读 inbox/request.json，写产物，写 `.DONE` |
| `slow` | 先 sleep `STUB_DELAY` 秒再写 `.DONE`（用于测超时） |
| `quota` | 输出配额报错，写 `.FAILED`，退出码 1 |
| `banned` | 输出封禁报错，写 `.FAILED`，退出码 1 |
| `crash` | 直接非零退出，不写任何标记 |
| `exit_ok` | 正常退出码 0，但**不写** `.DONE`（测"进程退出但无标记"） |
| `hang` | 一直 sleep，不产出（测超时与进程树终止） |
| `bad_args` | 模拟命令参数错误（测环境类错误识别） |
| `echo_token` | 输出里带真实 token，用于验证脱敏 |
| `mcp` | 按 JSON-RPC 应答 stdin（用于验证探针的握手识别） |

用法（由 CliRunner 调用，不手工运行）：
    python stub_cli.py [--ignore-request-file]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _paths() -> tuple[Path, Path, Path, Path]:
    workspace = Path(os.environ.get("NCO_WORKSPACE", "."))
    artifact_dir = Path(os.environ.get("NCO_ARTIFACT_DIR", str(workspace / "artifacts")))
    done_marker = Path(os.environ.get("NCO_DONE_MARKER", str(workspace / ".DONE")))
    request = workspace / "inbox" / "request.json"
    return workspace, artifact_dir, done_marker, request


def _read_request(request: Path) -> dict[str, object]:
    if not request.is_file():
        return {}
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_artifacts(workspace: Path, artifact_dir: Path, request: dict[str, object]) -> list[str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    goal = str(request.get("goal", "(no goal)"))
    label = os.environ.get("NCO_ACCOUNT_LABEL", "unknown")
    task_id = os.environ.get("NCO_TASK_ID", "unknown")

    report = artifact_dir / "report.md"
    report.write_text(
        f"# 桩产物\n\n- task_id: {task_id}\n- account: {label}\n- goal: {goal}\n",
        encoding="utf-8",
    )
    outline = workspace / "outline.md"
    outline.write_text(f"# 大纲\n\n1. {goal}\n", encoding="utf-8")
    return [str(report), str(outline)]


def _finish(done_marker: Path, summary: str) -> None:
    done_marker.parent.mkdir(parents=True, exist_ok=True)
    done_marker.write_text(summary, encoding="utf-8")


def main(argv: list[str]) -> int:
    del argv  # 桩不解析参数；ignored-args 场景下也无所谓
    mode = os.environ.get("STUB_MODE", "ok")
    delay = float(os.environ.get("STUB_DELAY", "0"))
    workspace, artifact_dir, done_marker, request = _paths()

    label = os.environ.get("NCO_ACCOUNT_LABEL", "unknown")
    print(f"[stub] 启动 mode={mode} account={label} ws={workspace}", flush=True)

    if mode == "bad_args":
        print("error: unrecognized arguments: --mcp-server", flush=True)
        return 2

    if mode == "mcp":
        # 模拟一个会说 JSON-RPC 的 MCP server：读一行 initialize，回一行 result。
        line = sys.stdin.readline() if not sys.stdin.isatty() else ""
        if line.strip():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = {}
            response = {
                "jsonrpc": "2.0",
                "id": parsed.get("id", 1),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "stub", "version": "0.1.0"},
                },
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        print(json.dumps(response), flush=True)
        time.sleep(0.5)
        return 0

    if mode == "hang":
        # 模拟常驻 CLI 卡死：一直不产出，靠外层超时与进程树终止收场。
        while True:
            time.sleep(0.2)

    if mode == "crash":
        print("Traceback (most recent call last):", flush=True)
        print("RuntimeError: stub crash before writing markers", flush=True)
        return 1

    payload = _read_request(request)
    if mode == "ok" and not payload:
        # 协议要求：CLI 必须能读到 inbox/request.json；读不到就是环境/协议问题。
        print("[stub] 缺少 inbox/request.json", file=sys.stderr, flush=True)
        return 3

    if mode == "echo_token":
        # 故意打印形似凭据的内容：验证 CliRunner 落盘前会脱敏。
        print("Authorization: Bearer stub-token-abcdef1234567890", flush=True)
        print("api_key=sk-stubsecret9876543210", flush=True)

    if delay:
        time.sleep(delay)

    if mode in {"quota", "banned"}:
        message = (
            "Error: You have exceeded your current quota. usage limit reached."
            if mode == "quota"
            else "Error: account is suspended due to policy violation."
        )
        print(message, flush=True)
        failed = workspace / ".FAILED"
        failed.write_text(message, encoding="utf-8")
        return 1

    if mode == "exit_ok":
        print("[stub] 正常退出但未写 .DONE", flush=True)
        return 0

    written = _write_artifacts(workspace, artifact_dir, payload)
    print(f"[stub] 产物 {len(written)} 个", flush=True)
    _finish(done_marker, f"stub 完成：{payload.get('goal', '')}")
    print("[stub] 已写 .DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
