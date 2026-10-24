"""上游更新测试：备份 → 同步 → 校验 → 失败回滚。

关键断言：
- 更新失败时**必须**回滚到原内容；
- 回滚后版本文件也恢复；
- 备份不保留超过配置份数。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from native_cli_orchestrator.config.settings import UpstreamSettings
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.upstream.updater import (
    REQUIRED_FILES,
    VENDOR_DIR,
    VERSION_FILE,
    UpstreamUpdater,
)


def _make_upstream(tmp_path: Path, *, valid: bool = True) -> Path:
    """造一个"上游仓库"目录。"""
    repo = tmp_path / "upstream-repo"
    schemas = repo / "schemas"
    schemas.mkdir(parents=True)
    payload = {"tools": ["submit_task", "get_task_result"]}
    if valid:
        (schemas / "tools.json").write_text(json.dumps(payload), encoding="utf-8")
    else:
        (schemas / "tools.json").write_text("{broken json", encoding="utf-8")
    (repo / "cli" / "README.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "cli" / "README.md").write_text("CLI v2\n", encoding="utf-8")
    return repo


def _updater(tmp_path: Path, repo: Path, **kwargs) -> tuple[UpstreamUpdater, Path]:
    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    updater = UpstreamUpdater(
        settings=UpstreamSettings(
            enabled=True, repo=str(repo), ref="main", keep_backups=2, verify_after_update=True, **kwargs
        ),
        project_root=project_root,
        backup_root=tmp_path / "backups",
    )
    return updater, project_root


# --------------------------------------------------------------------------- #
# 正常更新
# --------------------------------------------------------------------------- #


def test_check_only_does_not_touch_files(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, project_root = _updater(tmp_path, repo)

    result = updater.update(check_only=True)

    assert result.updated is False
    assert result.to_version
    assert not (project_root / VENDOR_DIR).exists()
    assert not (project_root / VERSION_FILE).exists()


def test_update_copies_files_and_writes_version(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, project_root = _updater(tmp_path, repo)

    result = updater.update()

    assert result.updated is True
    assert result.compatible is True
    assert result.rolled_back is False
    for required in REQUIRED_FILES:
        assert (project_root / VENDOR_DIR / required).is_file()
    assert (project_root / VENDOR_DIR / "cli" / "README.md").read_text(encoding="utf-8") == "CLI v2\n"

    version = json.loads((project_root / VERSION_FILE).read_text(encoding="utf-8"))
    assert version["version"] == result.to_version
    assert "schemas/tools.json" in version["files"]


def test_second_update_same_version_is_noop(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, _ = _updater(tmp_path, repo)
    updater.update()

    second = updater.update()
    assert second.updated is False
    assert "已是最新版本" in second.notes


def test_force_update_ignores_version(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, _ = _updater(tmp_path, repo)
    updater.update()

    forced = updater.update(force=True)
    assert forced.updated is True


def test_backup_created_before_sync(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, _ = _updater(tmp_path, repo)
    updater.update()  # 第一次：安装

    result = updater.update(force=True)  # 第二次：备份里才会有内容

    backup = Path(result.backup_path)
    assert backup.is_dir()
    assert (backup / "backup.meta.json").is_file()
    assert (backup / VENDOR_DIR / "schemas" / "tools.json").is_file()


# --------------------------------------------------------------------------- #
# 失败与回滚
# --------------------------------------------------------------------------- #


def test_incompatible_upstream_rolls_back(tmp_path: Path) -> None:
    """上游把 tools.json 改成坏 JSON → 校验失败 → 自动回滚。"""
    good_repo = _make_upstream(tmp_path / "good")
    updater, project_root = _updater(tmp_path, good_repo)
    updater.update()
    original = (project_root / VENDOR_DIR / "schemas" / "tools.json").read_text(encoding="utf-8")

    bad_repo = _make_upstream(tmp_path / "bad", valid=False)
    bad_updater, _ = _updater(tmp_path, bad_repo)

    with pytest.raises(ToolError) as excinfo:
        bad_updater.update(force=True)

    assert excinfo.value.code is ErrorCode.UPSTREAM_INCOMPATIBLE
    assert excinfo.value.detail["rolled_back"] is True
    # 内容应已回到原状
    restored = (project_root / VENDOR_DIR / "schemas" / "tools.json").read_text(encoding="utf-8")
    assert restored == original


def test_missing_required_file_rolls_back(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    repo_schemas = repo / "schemas" / "tools.json"
    repo_schemas.unlink()  # 上游缺关键文件
    updater, project_root = _updater(tmp_path, repo)

    with pytest.raises(ToolError) as excinfo:
        updater.update()

    assert excinfo.value.code is ErrorCode.UPSTREAM_INCOMPATIBLE
    assert "缺少关键文件" in excinfo.value.message
    assert not (project_root / VENDOR_DIR / "schemas" / "tools.json").exists()


def test_missing_repo_config_reports_error(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    updater = UpstreamUpdater(
        settings=UpstreamSettings(enabled=True, repo=""),
        project_root=project_root,
        backup_root=tmp_path / "backups",
    )
    with pytest.raises(ToolError) as excinfo:
        updater.update()
    assert excinfo.value.code is ErrorCode.UPSTREAM_UPDATE_FAILED


def test_nonexistent_repo_reports_error(tmp_path: Path) -> None:
    updater, _ = _updater(tmp_path, tmp_path / "does-not-exist")
    with pytest.raises(ToolError) as excinfo:
        updater.update()
    assert excinfo.value.code is ErrorCode.UPSTREAM_UPDATE_FAILED


# --------------------------------------------------------------------------- #
# 备份轮转
# --------------------------------------------------------------------------- #


def test_keep_backups_prunes_old(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, _ = _updater(tmp_path, repo)

    for _ in range(4):
        updater.update(force=True)

    backups = [p for p in (tmp_path / "backups").iterdir() if p.is_dir() and p.name.startswith("upstream-")]
    assert len(backups) <= 2, f"keep_backups=2 但留下了 {len(backups)} 份"


def test_version_state_readable(tmp_path: Path) -> None:
    repo = _make_upstream(tmp_path)
    updater, _ = _updater(tmp_path, repo)
    updater.update()

    state = updater.current_version()
    assert state.version
    assert state.repo == str(repo)
    assert state.synced_at


__all__ = ["VENDOR_DIR", "VERSION_FILE"]
