"""命令行参数解析（sse / stdio 共用）。

优先级（高 → 低）：命令行显式参数 > 环境变量 > config.local.yaml > config.yaml > 内置默认。
"""

from __future__ import annotations

import argparse
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="native-cli-orchestrator",
        description="三家 CLI 账号池的聚合调度 MCP 服务（Trae / Qoder / WorkBuddy）",
    )
    parser.add_argument("-c", "--config", default=None, help="配置文件路径，默认 config/config.yaml")
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default=None,
        help="强制运行模式，覆盖配置文件的 mode",
    )
    parser.add_argument("--host", default=None, help="SSE 监听地址，覆盖配置")
    parser.add_argument("--port", type=int, default=None, help="SSE 监听端口，覆盖配置")
    parser.add_argument("--accounts", default=None, help="accounts.yml 路径，覆盖配置")
    parser.add_argument("--workspace", default=None, help="任务工作目录根，覆盖配置")
    parser.add_argument("--log-dir", default=None, help="日志目录；传空字符串表示不落文件")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="只打印最终生效配置并退出（排障用）",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="环境自检（检查依赖/配置/账号/CLI/端口），不启动服务",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="配合 --doctor：额外探测 API base_url 连通性",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="在项目根生成缺失的 config/accounts.yml 模板（已存在则不覆盖）",
    )
    return parser


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """把命令行显式给出的值转成配置覆盖字典（未给出的不覆盖）。"""
    overrides: dict[str, Any] = {}

    if args.mode:
        overrides["mode"] = args.mode
    if args.accounts:
        overrides["accounts_path"] = args.accounts
    if args.workspace:
        overrides["workspace_root"] = args.workspace
    if args.log_dir is not None:
        overrides["log_dir"] = args.log_dir or None

    server: dict[str, Any] = {}
    if args.host:
        server["host"] = args.host
    if args.port:
        server["port"] = args.port
    if server:
        overrides["server"] = server

    return overrides
