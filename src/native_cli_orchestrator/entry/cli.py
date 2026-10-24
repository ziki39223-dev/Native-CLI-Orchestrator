"""启动入口：SSE（主推）、stdio，以及三条运维子命令。

用法：
    python -m native_cli_orchestrator.entry                  # 默认 SSE
    python -m native_cli_orchestrator.entry sse --port 8770
    python -m native_cli_orchestrator.entry stdio
    python -m native_cli_orchestrator.entry --doctor         # 环境自检
    python -m native_cli_orchestrator.entry --doctor --deep  # 自检 + API 连通性
    python -m native_cli_orchestrator.entry --init           # 补齐缺失配置
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config.loader import load_settings
from ..config.settings import AppSettings
from ..contracts.errors import ToolError
from ..observability.logging import configure_logging, get_logger
from ..server.app import build_server
from .args import build_parser, overrides_from_args
from .doctor import format_report, run_doctor
from .init import format_init_result, init_project

_log = get_logger("dao.entry")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _project_root(config_path: str | None) -> Path:
    """确定"项目根"（config/ workspace/ scripts/ 的父目录）。

    判定顺序：
    1. 显式 ``--config`` 所在目录的父目录；
    2. 当前工作目录（若它看起来像项目根：有 pyproject.toml 或 config/）；
    3. 源码安装位置（src-layout 下 ``parents[3]``）。

    第 2 条是为了让用户在任意目录下 ``--init`` 也落到他期望的位置，
    而不是悄悄写进 pip 安装目录。
    """
    if config_path:
        return Path(config_path).resolve().parent.parent

    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() or (cwd / "config").is_dir():
        return cwd

    return Path(__file__).resolve().parents[3]


def run_doctor_command(args: argparse.Namespace) -> int:
    """执行自检并打印报告；有 ERROR 时返回非零（便于脚本判断）。"""
    report = run_doctor(config_path=args.config, deep=args.deep)
    print(format_report(report))
    return 0 if report.passed else 1


def run_init_command(args: argparse.Namespace) -> int:
    """补齐缺失的配置文件。"""
    result = init_project(_project_root(args.config))
    print(format_init_result(result))
    return 0


def _load(args: argparse.Namespace) -> AppSettings:
    return load_settings(args.config, overrides=overrides_from_args(args))


def _banner(settings: AppSettings) -> None:
    _log.info(
        "Native-CLI-Orchestrator 启动：mode=%s transport=%s accounts=%s workspace=%s",
        settings.mode.value,
        settings.transport.value,
        settings.accounts_path,
        settings.workspace_root,
    )


def run_sse(argv: list[str] | None = None) -> int:
    """启动 SSE 服务（阻塞直到退出信号）。"""
    args = _parse(argv)
    try:
        settings = _load(args)
    except ToolError as exc:
        print(f"[启动失败] {exc.code.value}: {exc.message}", file=sys.stderr)
        print(json.dumps(exc.detail, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    if args.print_config:
        print(settings.model_dump_json(indent=2))
        return 0

    _banner(settings)
    server = build_server(settings)
    _log.info(
        "SSE 端点：http://%s:%d%s（消息端点：%s）",
        settings.server.host,
        settings.server.port,
        settings.server.sse_path,
        settings.server.message_path,
    )
    if settings.server.auth_token:
        _log.warning("已启用 auth_token：客户端需带 Authorization: Bearer <token>")

    try:
        server.run(
            transport="sse",
            host=settings.server.host,
            port=settings.server.port,
            sse_path=settings.server.sse_path,
            message_path=settings.server.message_path,
        )
    finally:
        _shutdown(server)
    return 0


def run_stdio(argv: list[str] | None = None) -> int:
    """启动 stdio 服务（stdout 只走协议，日志一律 stderr / 文件）。"""
    args = _parse(argv)
    try:
        settings = _load(args)
    except ToolError as exc:
        print(f"[启动失败] {exc.code.value}: {exc.message}", file=sys.stderr)
        return 2

    if args.print_config:
        print(settings.model_dump_json(indent=2))
        return 0

    configure_logging(
        level=settings.server.log_level,
        log_dir=settings.log_dir,
        to_stderr=True,
        force=True,
    )
    _banner(settings)
    server = build_server(settings)
    try:
        server.run(transport="stdio")
    finally:
        _shutdown(server)
    return 0


def _shutdown(server: object) -> None:
    """优雅退出：停调度器、杀子进程、存账号池状态。"""
    service = getattr(server, "_nco_service", None)
    if service is None:
        return
    try:
        service.shutdown()
    except Exception as exc:  # noqa: BLE001 - 退出路径不允许再抛
        _log.warning("退出清理失败：%s", exc)


def main(argv: list[str] | None = None) -> int:
    """子命令分派：``sse``（默认）/ ``stdio`` / ``doctor`` / ``init``。"""
    raw = list(sys.argv[1:] if argv is None else argv)

    command = "sse"
    if raw and raw[0] in {"sse", "stdio", "doctor", "init"}:
        command = raw.pop(0)

    if command in {"doctor", "init"}:
        args = _parse(raw)
        return run_doctor_command(args) if command == "doctor" else run_init_command(args)

    # --doctor / --init 也允许写在 sse/stdio 后面（例如 `entry sse --doctor`）。
    if "--doctor" in raw:
        return run_doctor_command(_parse([a for a in raw if a != "--doctor"]))
    if "--init" in raw:
        return run_init_command(_parse([a for a in raw if a != "--init"]))

    return run_stdio(raw) if command == "stdio" else run_sse(raw)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
