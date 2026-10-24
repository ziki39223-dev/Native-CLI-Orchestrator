"""自检（doctor）：把"工作机配好没配好"变成一条命令。

为什么需要它：需求是"工作机上只需配置一下"。但配置错了（key 没设、CLI 没装、
端口被占、mode 忘了改 real），现状只会得到一堆运行期报错，人得逐个试。
这个模块把所有前置条件一次查完，并明确告诉"下一步做什么"。

设计原则：
- **只读**：不写配置、不启服务、不发业务请求（`--deep` 只发一个 `/models` 探测）；
- **不泄露**：只报告"环境变量是否已设置"，绝不打印值；
- **分级**：ERROR（跑不起来）/ WARN（能跑但很可能不是你想要的）/ OK；
- **给动作**：每条问题都带一句"怎么修"。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..config.accounts import AccountsConfig, load_accounts
from ..config.loader import load_settings
from ..config.settings import AppSettings
from ..contracts.constants import Mode
from ..contracts.errors import ToolError


class Level(StrEnum):
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass(slots=True)
class Finding:
    """一条检查结论。"""

    level: Level
    area: str
    message: str
    #: 怎么修（ERROR/WARN 必填，OK 可空）。
    action: str = ""


@dataclass(slots=True)
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)
    #: 汇总的"下一步"清单，直接展示给用户。
    next_steps: list[str] = field(default_factory=list)

    def add(self, level: Level, area: str, message: str, action: str = "") -> None:
        self.findings.append(Finding(level=level, area=area, message=message, action=action))

    def ok(self, area: str, message: str) -> None:
        self.add(Level.OK, area, message)

    def warn(self, area: str, message: str, action: str) -> None:
        self.add(Level.WARN, area, message, action)

    def error(self, area: str, message: str, action: str) -> None:
        self.add(Level.ERROR, area, message, action)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.WARN]

    @property
    def passed(self) -> bool:
        """没有 ERROR 就算通过（WARN 不影响启动）。"""
        return not self.errors


# --------------------------------------------------------------------------- #
# 检查项
# --------------------------------------------------------------------------- #

#: 三家 CLI 的默认可执行名（与 constants.DEFAULT_CLI_COMMANDS 对齐）。
_REQUIRED_PY = (3, 12)


def _check_python(report: DoctorReport) -> None:
    version = sys.version_info
    if version >= _REQUIRED_PY:
        report.ok("python", f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        report.error(
            "python",
            f"Python {version.major}.{version.minor} 过低（需要 >= 3.12）",
            "安装 Python 3.12+ 后重建 venv，或改用 exe 版本",
        )


def _check_dependencies(report: DoctorReport) -> None:
    required = {
        "mcp": "mcp",
        "pydantic": "pydantic",
        "yaml": "PyYAML",
        "httpx2": "httpx2",
    }
    missing: list[str] = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        report.error(
            "deps",
            f"缺少依赖：{', '.join(missing)}",
            "运行 scripts\\setup.bat，或 pip install -r requirements.txt",
        )
    else:
        report.ok("deps", "运行依赖齐全（mcp / pydantic / PyYAML / httpx2）")


def _check_settings(report: DoctorReport, config_path: Path | None) -> AppSettings | None:
    try:
        settings = load_settings(config_path)
    except ToolError as exc:
        report.error(
            "config",
            f"config.yaml 无法加载：{exc.message}",
            "检查 config\\config.yaml 的 YAML 语法与字段；用 --print-config 看解析结果",
        )
        if exc.detail.get("reason"):
            report.warn("config", f"详细原因：{str(exc.detail['reason'])[:300]}", "")
        return None

    report.ok("config", f"config.yaml 已加载（mode={settings.mode.value}）")
    if settings.mode is Mode.MOCK:
        report.warn(
            "config",
            "当前是 mock 模式：任务返回的是模拟数据，不会真的调用 CLI 或 API",
            "确认要真机运行时，把 config\\config.yaml 的 mode 改成 real",
        )
    return settings


def _check_accounts(report: DoctorReport, settings: AppSettings) -> AccountsConfig | None:
    if not settings.accounts_path.is_file():
        report.error(
            "accounts",
            f"账号池配置不存在：{settings.accounts_path}",
            "复制 config\\accounts.yml 模板并填写账号（或从交付包解压完整目录）",
        )
        return None

    try:
        accounts = load_accounts(settings.accounts_path)
    except ToolError as exc:
        report.error(
            "accounts",
            f"accounts.yml 校验失败：{exc.message}",
            "按错误里的字段路径修正；分组名须以字母开头，kind=api 必须有 api 节",
        )
        for err in exc.detail.get("errors", [])[:5]:
            report.warn("accounts", f"  ↳ {err.get('loc')}: {err.get('msg')}", "")
        return None

    api_count = len(accounts.api_group_names)
    report.ok(
        "accounts",
        f"accounts.yml 已加载：{len(accounts.groups)} 组 / {accounts.account_count} 账号"
        + (f"（其中 {api_count} 个 API 组）" if api_count else ""),
    )
    return accounts


def _check_cli_groups(
    report: DoctorReport, accounts: AccountsConfig, settings: AppSettings
) -> None:
    for name in accounts.cli_group_names:
        group = accounts.group(name)
        if all(acc.disabled for acc in group.accounts):
            report.ok("cli", f"{name}：全部账号 disabled，跳过检查")
            continue
        try:
            command = accounts.command_for(name)
        except ToolError as exc:
            report.error("cli", f"{name}：{exc.message}", "在 accounts.yml 里补 cli 字段")
            continue

        executable = shutil.which(command[0])
        if executable is None:
            if settings.mode is Mode.MOCK:
                report.warn(
                    "cli",
                    f"{name}：找不到命令 {command[0]}（mock 模式下不影响）",
                    "真机运行前请安装该 CLI，或把安装目录加进 PATH；"
                    "用 python devtools\\probe_cli.py 摸清真实命令",
                )
            else:
                report.error(
                    "cli",
                    f"{name}：找不到命令 {command[0]}",
                    "安装该 CLI 并确认在 PATH 中；真实命令与默认不符时改 accounts.yml 的 cli 字段"
                    "（先跑 python devtools\\probe_cli.py）",
                )
            continue
        report.ok("cli", f"{name}：{command[0]} 已找到（{' '.join(command)}）")


def _check_api_groups(
    report: DoctorReport, accounts: AccountsConfig, environ: dict[str, str]
) -> list[tuple[str, str]]:
    """检查 API 组；返回可做连通性探测的 (分组, base_url) 列表。"""
    probes: list[tuple[str, str]] = []
    for name in accounts.api_group_names:
        group = accounts.group(name)
        spec = group.api
        if spec is None:  # 防御：配置层已保证非空
            continue
        report.ok("api", f"{name}：{spec.base_url}（model={spec.model}）")

        if spec.daily_budget <= 0:
            report.warn(
                "api",
                f"{name}：未设日预算（daily_budget=0），会一直花钱不拦",
                "付费供应商建议设 daily_budget；免费网关可忽略",
            )
        elif not spec.priced:
            report.warn(
                "api",
                f"{name}：设了预算 {spec.daily_budget} 元但没配价格，无法估算花费",
                "填 input_price_per_mtok / output_price_per_mtok，否则预算永远不会触发",
            )
        else:
            report.ok(
                "api",
                f"{name}：日预算 {spec.daily_budget} 元，单价 入{spec.input_price_per_mtok}"
                f"/出{spec.output_price_per_mtok} 元每百万 token",
            )

        # 账号 key 检查（只报"是否设置"，不打印值）
        for index, account in enumerate(group.accounts):
            if account.disabled:
                continue
            try:
                account.api_key(environ)
            except ToolError as exc:
                report.error(
                    "api",
                    f"{name}[{index}] {account.label}：{exc.message}",
                    "用 setx 设置该环境变量后重开终端；accounts.yml 里只写 env:变量名",
                )
            else:
                report.ok("api", f"{name}[{index}] {account.label}：API key 已就绪")

        if not any(acc.disabled for acc in group.accounts):
            probes.append((name, spec.base_url))
    return probes


def _port_in_use(port: int, *, host: str = "127.0.0.1") -> bool:
    """判断端口是否已被占用。

    为什么不用 ``bind()`` 判断：Windows 上 ``SO_REUSEADDR`` 允许绑定已被监听的端口，
    所以 bind 成功不代表端口空闲（这正是我们第一次实现时的 bug）。
    改用"先尝试连接"：能连上就说明有服务在听。
    """
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104 - 仅字符串比较

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.35)
        if client.connect_ex((probe_host, port)) == 0:
            return True

    # 连不上时再尝试独占绑定（不加 SO_REUSEADDR），确认没有半占用。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        try:
            server.bind((probe_host, port))
        except OSError:
            return True
    return False


def _check_port(report: DoctorReport, settings: AppSettings) -> None:
    if settings.transport.value != "sse":
        report.ok("port", f"传输是 {settings.transport.value}，不占端口")
        return

    host, port = settings.server.host, settings.server.port
    if _port_in_use(port, host=host):
        report.error(
            "port",
            f"端口 {host}:{port} 已被占用",
            f"换端口（--port 8771）或关掉占用该端口的进程（netstat -ano | findstr {port}）",
        )
    else:
        report.ok("port", f"端口 {host}:{port} 可用")

    if host == "0.0.0.0" and not settings.server.auth_token:  # noqa: S104 - 仅比较字符串
        report.warn(
            "port",
            "监听 0.0.0.0 但没设 auth_token：局域网内任何人都能调用",
            "设置 server.auth_token，客户端带 Authorization: Bearer <token>",
        )


def _check_workspace(report: DoctorReport, settings: AppSettings) -> None:
    root = settings.workspace_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.error(
            "workspace",
            f"工作目录不可写：{root}（{exc}）",
            "换到有写权限的目录（--workspace D:\\nco-workspace）",
        )
    else:
        report.ok("workspace", f"工作目录可写：{root}")


def _check_upstream(report: DoctorReport, settings: AppSettings) -> None:
    if not settings.upstream.enabled:
        report.ok("upstream", "上游更新已关闭")
        return
    if not settings.upstream.repo:
        report.warn(
            "upstream",
            "上游更新已启用但没配 repo：update_upstream 会报 UPSTREAM_UPDATE_FAILED",
            "要么填 upstream.repo，要么把 upstream.enabled 改成 false",
        )
        return
    report.ok("upstream", f"上游：{settings.upstream.repo}（ref={settings.upstream.ref}）")


# --------------------------------------------------------------------------- #
# 连通性探测（--deep）
# --------------------------------------------------------------------------- #


async def _probe_base_url(base_url: str, timeout_s: float = 8.0) -> tuple[bool, str]:
    """探测 {base_url}/models 是否可达。

    用 /models 而不是 chat/completions：不花钱、不需要模型权限，只看网络与 base_url 是否正确。
    401/403 也算"可达"（说明地址对、只是 key 问题或该端点不公开）。
    """
    import httpx2

    url = f"{base_url.rstrip('/')}/models"
    client = httpx2.AsyncClient(timeout=timeout_s, follow_redirects=True)
    try:
        response = await client.get(url)
    except httpx2.HTTPError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        await client.aclose()

    if response.status_code < 400:
        return True, f"HTTP {response.status_code}"
    if response.status_code in {401, 403}:
        return True, f"HTTP {response.status_code}（地址可达，鉴权由调用时决定）"
    if response.status_code == 404:
        return False, "HTTP 404（base_url 可能少/多了 /v1，或不支持 /models）"
    return False, f"HTTP {response.status_code}"


def _deep_probe(report: DoctorReport, targets: list[tuple[str, str]]) -> None:
    import asyncio

    for name, base_url in targets:
        try:
            ok, detail = asyncio.run(_probe_base_url(base_url))
        except Exception as exc:  # noqa: BLE001 - 探测失败不应让 doctor 崩
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        if ok:
            report.ok("api-probe", f"{name}：{base_url} 可达（{detail}）")
        else:
            report.error(
                "api-probe",
                f"{name}：{base_url} 不可达（{detail}）",
                "检查 base_url 是否带正确的 /v1 前缀、网络/代理是否通、网关是否在线",
            )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run_doctor(
    *,
    config_path: str | Path | None = None,
    deep: bool = False,
    environ: dict[str, str] | None = None,
) -> DoctorReport:
    """执行全部检查。``deep=True`` 时额外探测 API base_url 连通性。"""
    env = dict(os.environ if environ is None else environ)
    report = DoctorReport()

    _check_python(report)
    _check_dependencies(report)

    settings = _check_settings(report, Path(config_path) if config_path else None)
    if settings is None:
        report.next_steps.append("先修好 config\\config.yaml，再重跑 doctor")
        return report

    _check_workspace(report, settings)
    _check_port(report, settings)
    _check_upstream(report, settings)

    accounts = _check_accounts(report, settings)
    if accounts is None:
        report.next_steps.append("先修好 config\\accounts.yml（可从模板复制），再重跑 doctor")
        return report

    _check_cli_groups(report, accounts, settings)
    api_targets = _check_api_groups(report, accounts, env)
    if deep and api_targets:
        _deep_probe(report, api_targets)

    _build_next_steps(report, settings, accounts, deep=deep)
    return report


def _build_next_steps(
    report: DoctorReport,
    settings: AppSettings,
    accounts: AccountsConfig,
    *,
    deep: bool,
) -> None:
    """把问题归纳成"按顺序做这几件事"。"""
    steps: list[str] = []

    if report.errors:
        steps.append("先解决上面所有 ERROR —— 有 ERROR 时服务无法正常启动")
    for finding in report.errors[:4]:
        if finding.action:
            steps.append(f"[{finding.area}] {finding.action}")

    if settings.mode is Mode.MOCK and not report.errors:
        steps.append(
            "确认要真机运行：把 config\\config.yaml 的 mode 改成 real，然后重启服务"
        )

    if not deep and accounts.api_group_names and not report.errors:
        steps.append("想验证 API 地址是否可达：再跑一次 doctor --deep")

    if not steps:
        if settings.transport.value == "sse":
            steps.append(
                f"一切就绪。启动：scripts\\start_sse.bat （端点 http://{settings.server.host}:"
                f"{settings.server.port}{settings.server.sse_path}）"
            )
        else:
            steps.append("一切就绪。启动：scripts\\start_stdio.bat（由上层工作台接管）")

    report.next_steps = steps


def format_report(report: DoctorReport) -> str:
    """渲染成给人看的文本。"""
    symbols = {Level.OK: "[OK]   ", Level.WARN: "[WARN] ", Level.ERROR: "[ERROR]"}
    lines: list[str] = ["=" * 62, " Native-CLI-Orchestrator 环境自检", "=" * 62]

    area_order = ["python", "deps", "config", "workspace", "port", "upstream",
                  "accounts", "cli", "api", "api-probe"]
    grouped: dict[str, list[Finding]] = {}
    for finding in report.findings:
        grouped.setdefault(finding.area, []).append(finding)

    for area in area_order + [a for a in grouped if a not in area_order]:
        items = grouped.get(area)
        if not items:
            continue
        lines.append("")
        lines.append(f"— {area} —")
        for item in items:
            lines.append(f"{symbols[item.level]} {item.message}")
            if item.action and item.level is not Level.OK:
                lines.append(f"          → {item.action}")

    lines.append("")
    lines.append("=" * 62)
    errors, warns = len(report.errors), len(report.warnings)
    if report.passed:
        lines.append(f" 结论：通过（{warns} 条提醒）" if warns else " 结论：全部通过")
    else:
        lines.append(f" 结论：未通过（{errors} 个 ERROR，{warns} 条 WARN）")
    if report.next_steps:
        lines.append("")
        lines.append(" 下一步：")
        for index, step in enumerate(report.next_steps, start=1):
            lines.append(f"   {index}. {step}")
    lines.append("=" * 62)
    return "\n".join(lines)


__all__ = [
    "DoctorReport",
    "Finding",
    "Level",
    "format_report",
    "run_doctor",
]
