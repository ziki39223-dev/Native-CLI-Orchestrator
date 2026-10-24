"""配置层：``config.yaml``（全局）与 ``accounts.yml``（账号池）。

安全口径：**凭据本身不入配置文件**，配置里只写环境变量名（``env:`` 字段），
由子进程管理器在拉起 CLI 时注入。
"""

from __future__ import annotations

from .accounts import (
    AccountConfig,
    AccountsConfig,
    GroupConfig,
    load_accounts,
    parse_accounts,
)
from .loader import load_settings, resolve_config_path
from .settings import AppSettings, RunnerSettings, ServerSettings, UpstreamSettings

__all__ = [
    "AccountConfig",
    "AccountsConfig",
    "AppSettings",
    "GroupConfig",
    "RunnerSettings",
    "ServerSettings",
    "UpstreamSettings",
    "load_accounts",
    "load_settings",
    "parse_accounts",
    "resolve_config_path",
]
