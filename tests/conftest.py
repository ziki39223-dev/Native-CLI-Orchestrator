"""测试公共夹具。

所有夹具都在 Mock 模式或 Fake 执行器下工作，**不依赖任何真实 CLI**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from native_cli_orchestrator.config.accounts import AccountsConfig, parse_accounts
from native_cli_orchestrator.config.settings import (
    AppSettings,
    RunnerSettings,
    ServerSettings,
    UpstreamSettings,
)

ACCOUNTS_DOCUMENT: dict[str, Any] = {
    "config_version": "test-1.0",
    "groups": {
        "trae_group": {
            "accounts": [
                {"label": "trae-a", "env": {"PROFILE": "alpha"}},
                {"label": "trae-b", "env": {"PROFILE": "beta"}},
                {"label": "trae-c", "env": {"PROFILE": "gamma"}},
            ]
        },
        "qoder_group": {
            "accounts": [
                {"label": "qoder-a", "env": {"PROFILE": "qa"}},
                {"label": "qoder-b", "env": {"PROFILE": "qb"}},
            ]
        },
        "workbuddy_group": {
            "accounts": [
                {"label": "wb-a", "env": {"PROFILE": "wa"}},
                {"label": "wb-b", "env": {"PROFILE": "wb"}, "disabled": True},
            ]
        },
    },
}


@pytest.fixture
def accounts_document() -> dict[str, Any]:
    import copy

    return copy.deepcopy(ACCOUNTS_DOCUMENT)


@pytest.fixture
def accounts(accounts_document: dict[str, Any]) -> AccountsConfig:
    return parse_accounts(accounts_document)


@pytest.fixture
def accounts_file(tmp_path: Path, accounts_document: dict[str, Any]) -> Path:
    import yaml

    path = tmp_path / "accounts.yml"
    path.write_text(yaml.safe_dump(accounts_document, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def settings_mock(tmp_path: Path, accounts_file: Path) -> AppSettings:
    """Mock 模式配置，冷却时间压到 0 以便测试快速跑。"""
    return AppSettings(
        mode="mock",  # type: ignore[arg-type]
        transport="stdio",  # type: ignore[arg-type]
        accounts_path=accounts_file,
        workspace_root=tmp_path / "workspace",
        backup_root=tmp_path / "backups",
        log_dir=None,
        server=ServerSettings(log_level="WARNING"),
        runner=RunnerSettings(
            default_timeout_s=5.0,
            max_account_attempts=3,
            poll_interval_s=0.05,
            group_cooldown_s=0.0,
            switch_cooldown_s=0.0,
            failure_threshold=2,
        ),
        upstream=UpstreamSettings(enabled=False),
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免开发机上的真实凭据影响测试（env:xxx 引用会失败）。"""
    for name in (
        "TRAE_TOKEN_1",
        "TRAE_TOKEN_2",
        "QODER_TOKEN_1",
        "WORKBUDDY_TOKEN_1",
        "NCO_TEST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
