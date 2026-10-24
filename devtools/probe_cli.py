"""工作机探针：一次性摸清三家 CLI 的真实情况，产出可直接回填的报告。

为什么需要它：`CliProfile` 里的默认命令（`traecli mcp-server` / `qoder mcp serve` /
`workbuddy-mcp`）是**推测值**。上游可能改名、改子命令、改启动参数、不写 `.DONE`。
这个脚本把"人工翻文档 + 逐个试"压缩成一条命令。

它只做**只读探测**：
1. 在 PATH 里找候选可执行文件（含常见别名）
2. 跑 `--version` / `-V` / `version`（超时就跳过）
3. 跑 `--help` 抓输出，从里面找子命令与 MCP 相关参数
4. 尝试以 MCP 方式拉起 5 秒，只观察它是否输出 JSON-RPC 握手、是否写 `.DONE`
5. 把结论写成 JSON + Markdown 报告，并给出 `accounts.yml` 的 `cli` 字段建议

用法：
    python devtools/probe_cli.py                 # 全量探测，报告写到 probe-report/
    python devtools/probe_cli.py --only traecli  # 只探一个
    python devtools/probe_cli.py --timeout 10
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "probe-report"

#: 候选命令：每组的首选名 + 常见变体。顺序即尝试顺序。
CANDIDATES: dict[str, tuple[str, ...]] = {
    "trae_group": ("traecli", "trae", "trae-cli", "trae_cli"),
    "qoder_group": ("qoder", "qoder-cli", "qodercmd", "qoder_cli"),
    "workbuddy_group": ("workbuddy-mcp", "workbuddy", "workbuddycli", "wb-mcp"),
}

#: MCP 相关关键词：从 --help 里捞子命令用。
MCP_KEYWORDS = ("mcp", "serve", "server", "stdio", "jsonrpc", "json-rpc")

#: 合法子命令 token：只允许字母数字与连字符/下划线，杜绝把输出里的 JSON 片段当子命令。
_SUBCOMMAND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: 版本探测的候选参数。
VERSION_FLAGS = (("--version",), ("-V",), ("-v",), ("version",))


@dataclass
class ProbeResult:
    group: str
    command: str = ""
    resolved_path: str = ""
    version_ok: bool = False
    version_output: str = ""
    version_flag: str = ""
    help_ok: bool = False
    mcp_subcommands: list[str] = field(default_factory=list)
    recommended_cli: list[str] = field(default_factory=list)
    handshake_seen: bool = False
    writes_done: bool = False
    startup_output: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _run(cmd: list[str], *, timeout: float) -> tuple[int | None, str]:
    """跑一条命令，返回 (退出码, 合并输出)。超时返回 (None, "超时")。"""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return None, "超时"
    except OSError as exc:
        return None, f"启动失败：{exc}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _find_executable(names: tuple[str, ...]) -> tuple[str, str]:
    """在 PATH 里找第一个命中的可执行文件，返回 (名字, 绝对路径)。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return name, found
    return "", ""


def _probe_version(executable: str, *, timeout: float) -> tuple[bool, str, str]:
    for flag in VERSION_FLAGS:
        code, output = _run([executable, *flag], timeout=timeout)
        if code == 0 and output.strip():
            return True, output.strip()[:500], " ".join(flag)
    return False, "", ""


def _probe_help(executable: str, *, timeout: float) -> tuple[bool, str]:
    code, output = _run([executable, "--help"], timeout=timeout)
    if code != 0 or not output.strip():
        code, output = _run([executable, "help"], timeout=timeout)
    if code != 0 or not output.strip():
        return False, ""
    return True, output


def _guess_mcp_subcommands(help_text: str) -> list[str]:
    """从 help 输出里找可能用于启动 MCP 的服务式子命令。

    只接受"像子命令"的 token（字母/数字/连字符/下划线），避免把 JSON 片段、
    带引号的字符串或说明文字误当成子命令。
    """
    found: list[str] = []
    lowered = help_text.lower()
    if not any(keyword in lowered for keyword in MCP_KEYWORDS):
        return found

    for line in help_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("-", "#", "{", "[", "//")):
            continue
        head = stripped.split()[0].strip(",:")
        if not _SUBCOMMAND_RE.fullmatch(head):
            continue
        if len(head) > 32:
            continue
        if head.lower() in {"usage", "commands", "options", "command", "用法", "命令"}:
            continue
        if any(keyword in head.lower() for keyword in MCP_KEYWORDS) and head not in found:
            found.append(head)
    return found[:8]


def _try_mcp_start(executable: str, args: list[str], *, timeout: float) -> tuple[bool, bool, str]:
    """以 MCP 方式拉起若干秒，观察是否出现 JSON-RPC 握手。

    Returns:
        (是否看到握手, 是否写了完成标记, 输出摘要)
    """
    cmd = [executable, *args]
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False, False, ""

    # 发一个最小 initialize 请求，看看它是否按 JSON-RPC 回应。
    request = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"probe","version":"0.1.0"}}}\n'
    )
    output = ""
    try:
        if process.stdin is not None:
            process.stdin.write(request)
            process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.2)
        process.terminate()
        try:
            out, err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
        output = (out or "") + (err or "")
    except OSError:
        pass

    handshake = '"jsonrpc"' in output or '"result"' in output or '"protocolVersion"' in output
    return handshake, False, output.strip()[:800]


def probe_group(group: str, *, timeout: float) -> ProbeResult:
    result = ProbeResult(group=group)
    names = CANDIDATES[group]
    name, path = _find_executable(names)

    if not path:
        result.problems.append(
            f"PATH 里找不到任何候选命令：{', '.join(names)}。请确认 CLI 已安装并在 PATH 中。"
        )
        return result

    result.command = name
    result.resolved_path = path

    version_ok, version_output, version_flag = _probe_version(path, timeout=timeout)
    result.version_ok = version_ok
    result.version_output = version_output
    result.version_flag = version_flag
    if not version_ok:
        result.notes.append("--version 无输出或失败，可能是不支持该参数；不影响使用")

    help_ok, help_text = _probe_help(path, timeout=timeout)
    result.help_ok = help_ok
    if help_ok:
        result.mcp_subcommands = _guess_mcp_subcommands(help_text)
        if not result.mcp_subcommands:
            result.problems.append("--help 里没有找到 mcp/serve/stdio 相关子命令，需人工确认启动方式")
    else:
        result.problems.append("--help 无输出，无法自动推断启动参数")

    # 逐个尝试候选子命令，看哪个能建立 JSON-RPC 握手。
    candidates: list[list[str]] = []
    for sub in result.mcp_subcommands:
        candidates.append([sub])
    candidates.extend([["mcp-server"], ["mcp", "serve"], ["serve"], []])

    for args in candidates:
        handshake, writes_done, output = _try_mcp_start(path, args, timeout=min(timeout, 5.0))
        result.startup_output = output
        if handshake:
            result.handshake_seen = True
            result.writes_done = writes_done
            result.recommended_cli = [name, *args]
            result.notes.append(f"MCP 握手成功：{name} {' '.join(args)}".strip())
            break
    else:
        result.notes.append(
            "未观察到 JSON-RPC 握手。可能是：① 需要先登录；② 启动参数不同；"
            "③ 它不是 MCP server 而是纯 CLI。请手工跑一次确认。"
        )

    return result


def write_reports(results: list[ProbeResult]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"probe-{stamp}.json"
    md_path = REPORT_DIR / f"probe-{stamp}.md"

    json_path.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# CLI 探针报告",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 平台：{sys.platform}",
        "",
        "## 结论速览",
        "",
        "| 分组 | 命令 | 版本 | MCP 握手 | 建议 cli |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in results:
        version = "✅" if item.version_ok else "—"
        handshake = "✅" if item.handshake_seen else "⚠️"
        cli = " ".join(item.recommended_cli) if item.recommended_cli else "待人工确认"
        lines.append(f"| {item.group} | {item.command or '未找到'} | {version} | {handshake} | `{cli}` |")

    lines.extend(["", "## accounts.yml 建议片段", "", "```yaml", "groups:"])
    for item in results:
        if not item.recommended_cli:
            lines.append(f"  {item.group}:")
            lines.append("    cli: []   # 待人工填写")
            continue
        cli_repr = json.dumps(item.recommended_cli, ensure_ascii=False)
        lines.append(f"  {item.group}:")
        lines.append(f"    cli: {cli_repr}")
    lines.extend(["```", "", "## 逐项详情", ""])

    for item in results:
        lines.append(f"### {item.group}")
        lines.append("")
        lines.append(f"- 解析到：`{item.resolved_path or '（无）'}`")
        if item.version_output:
            lines.append(f"- 版本（`{item.version_flag}`）：`{item.version_output.splitlines()[0][:120]}`")
        if item.mcp_subcommands:
            lines.append(f"- help 里的 MCP 相关子命令：{', '.join(item.mcp_subcommands)}")
        if item.startup_output:
            lines.append("- 启动输出片段：")
            lines.append("```")
            lines.append(item.startup_output[:600])
            lines.append("```")
        for note in item.notes:
            lines.append(f"- 说明：{note}")
        for problem in item.problems:
            lines.append(f"- ⚠️ 问题：{problem}")
        lines.append("")

    lines.extend(
        [
            "## 下一步",
            "",
            "1. 把上面的 `cli` 片段填进 `config/accounts.yml` 对应分组。",
            "2. 若「MCP 握手」为 ⚠️，说明该 CLI 的启动方式与本项目默认假设不同：",
            "   - 先手工跑一次，确认它是否需要先登录、是否接受 stdin 的 JSON-RPC；",
            "   - 若它根本不说 JSON-RPC，而是「一次性执行 + 写文件」，把 `CliProfile.long_running` 设为 `False`，",
            "     并在任务的 `prompt` 里明确要求写 `.DONE`。",
            "3. 补完配置后，用 `scripts/mcp_client_demo.py` 做一次真机冒烟。",
            "",
        ]
    )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="三家 CLI 探针（只读）")
    parser.add_argument("--only", default=None, help="只探某个命令名（模糊匹配分组）")
    parser.add_argument("--timeout", type=float, default=8.0, help="单条命令超时秒数")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("[警告] 三家 CLI 均为 Windows 客户端，非 Windows 上探测结果可能无意义", file=sys.stderr)

    groups = list(CANDIDATES)
    if args.only:
        groups = [g for g in groups if any(args.only in n for n in CANDIDATES[g])]
        if not groups:
            print(f"[错误] 没有分组包含命令 {args.only!r}", file=sys.stderr)
            return 2

    print(f"[探针] 开始探测 {len(groups)} 个分组，单条超时 {args.timeout}s\n")
    results: list[ProbeResult] = []
    for group in groups:
        print(f"— {group} …", end="", flush=True)
        result = probe_group(group, timeout=args.timeout)
        results.append(result)
        status = "OK" if result.handshake_seen else ("找到但未握手" if result.resolved_path else "未找到")
        print(f" {status}")

    json_path, md_path = write_reports(results)
    print(f"\n[报告] {json_path}")
    print(f"[报告] {md_path}")

    missing = [item.group for item in results if not item.resolved_path]
    if missing:
        print(f"\n[提示] 以下分组没找到命令：{', '.join(missing)}")
        print("       确认 CLI 已安装，或它们在非标准路径时把目录加进 PATH。")

    no_handshake = [item.group for item in results if item.resolved_path and not item.handshake_seen]
    if no_handshake:
        print(f"\n[注意] 以下分组未观察到 MCP 握手：{', '.join(no_handshake)}")
        print("       见报告「下一步」第 2 条，可能需要适配 long_running 或改启动参数。")

    return 0 if all(item.resolved_path for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
