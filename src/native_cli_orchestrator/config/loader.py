"""``config.yaml`` 加载：合并顺序 + 环境变量覆盖 + 路径解析。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ..contracts.errors import ErrorCode, ToolError
from .settings import AppSettings

#: 允许用环境变量覆盖的键（仅这些，避免环境变量成为隐形契约）。
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "NCO_MODE": ("mode",),
    "NCO_TRANSPORT": ("transport",),
    "NCO_HOST": ("server", "host"),
    "NCO_PORT": ("server", "port"),
    "NCO_AUTH_TOKEN": ("server", "auth_token"),
    "NCO_LOG_LEVEL": ("server", "log_level"),
    "NCO_ACCOUNTS": ("accounts_path",),
    "NCO_WORKSPACE": ("workspace_root",),
}

_DEFAULT_CANDIDATES = ("config/config.yaml", "config.yaml")


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """确定配置文件路径：显式参数 > ``config/config.yaml`` > ``config.yaml``。"""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"配置文件不存在：{path}",
                detail={"path": str(path)},
            )
        return path

    for candidate in _DEFAULT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path

    raise ToolError(
        ErrorCode.CONFIG_INVALID,
        "找不到配置文件，请提供 --config 或创建 config/config.yaml",
        detail={"searched": list(_DEFAULT_CANDIDATES)},
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"配置文件 YAML 语法错误：{path}",
            detail={"path": str(path), "reason": str(exc)},
        ) from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"配置文件顶层必须是映射结构：{path}",
            detail={"path": str(path), "actual_type": type(raw).__name__},
        )
    return raw


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _ensure_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    child = parent.get(key)
    if child is None:
        child = {}
        parent[key] = child
    if not isinstance(child, dict):
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"配置节 ``{key}`` 必须是映射结构",
            detail={"key": key, "actual_type": type(child).__name__},
        )
    return child


def _apply_env_overrides(document: dict[str, Any], environ: dict[str, str]) -> None:
    for env_key, path in ENV_OVERRIDES.items():
        value = environ.get(env_key)
        if value is None or value == "":
            continue
        node = document
        for part in path[:-1]:
            node = _ensure_mapping(node, part)
        leaf = path[-1]
        if leaf == "port":
            try:
                node[leaf] = int(value)
            except ValueError as exc:
                raise ToolError(
                    ErrorCode.CONFIG_INVALID,
                    f"环境变量 {env_key} 必须是整数",
                    detail={"value": value},
                ) from exc
        else:
            node[leaf] = value


def _absolute(path: Path, project_root: Path) -> Path:
    return path if path.is_absolute() else (project_root / path).resolve()


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppSettings:
    """加载并校验全局配置。

    相对路径按「配置文件所在目录的父目录」（即项目根）解析，保证从任意 cwd 启动都正确。
    """
    path = resolve_config_path(config_path)
    document = _read_yaml(path)
    project_root = path.resolve().parent.parent

    local_path = path.parent / "config.local.yaml"
    if local_path.is_file():
        document = _deep_merge(document, _read_yaml(local_path))

    _apply_env_overrides(document, environ if environ is not None else dict(os.environ))
    if overrides:
        document = _deep_merge(document, overrides)

    try:
        settings = AppSettings.model_validate(document)
    except Exception as exc:
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            "config.yaml 校验失败",
            detail={"path": str(path), "reason": str(exc)},
        ) from exc

    return settings.model_copy(
        update={
            "accounts_path": _absolute(settings.accounts_path, project_root),
            "workspace_root": _absolute(settings.workspace_root, project_root),
            "backup_root": _absolute(settings.backup_root, project_root),
            "log_dir": _absolute(settings.log_dir, project_root) if settings.log_dir else None,
        }
    )
