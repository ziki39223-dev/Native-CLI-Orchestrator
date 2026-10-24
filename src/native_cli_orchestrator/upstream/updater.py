"""上游同步：拉取上游仓库 → 备份 → 同步 → 兼容性校验 → 失败回滚。

安全底线（需求 §5「约束与风险 2」）：
- **先备份再动文件**，任何一步失败都自动回滚；
- 不做 ``git reset --hard`` 到用户工作区，只对 ``vendor/`` 目标子树做替换；
- 备份目录独立存放，保留 N 份。

流程::

    check   → 读本地版本 → 读上游版本 → 比较（无副作用）
    update  → 备份 → 拉取 → 覆盖目标子树 → 校验 → (失败) 回滚
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from ..common.text import now_local
from ..config.settings import UpstreamSettings
from ..contracts.errors import ErrorCode, ToolError
from ..contracts.tools import UpdateUpstreamData
from ..observability.logging import get_logger

_log = get_logger("dao.upstream")

#: 上游需要同步到的目标目录（相对项目根）。
VENDOR_DIR: Final[str] = "vendor"
#: 上游版本清单文件名。
VERSION_FILE: Final[str] = "upstream.version.json"
#: 同步后必须存在的关键文件（兼容性校验的最小集合）。
REQUIRED_FILES: Final[tuple[str, ...]] = ("schemas/tools.json",)


@dataclass
class UpstreamState:
    """本地记录的上游版本信息。"""

    version: str = ""
    ref: str = ""
    repo: str = ""
    synced_at: str = ""
    files: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> UpstreamState:
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls(
            version=str(raw.get("version", "")),
            ref=str(raw.get("ref", "")),
            repo=str(raw.get("repo", "")),
            synced_at=str(raw.get("synced_at", "")),
            files=[str(item) for item in raw.get("files", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": self.version,
                    "ref": self.ref,
                    "repo": self.repo,
                    "synced_at": self.synced_at,
                    "files": self.files,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class UpstreamUpdater:
    """上游更新器。

    支持两种上游：
    - ``git`` 仓库（生产用法）；
    - 本地目录（开发机测流程用，直接拷贝）。
    """

    def __init__(
        self,
        *,
        settings: UpstreamSettings,
        project_root: Path,
        vendor_dir: Path | None = None,
        backup_root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._project_root = project_root
        self._vendor = vendor_dir or (project_root / VENDOR_DIR)
        self._backup_root = backup_root or (project_root / "backups")
        self._version_path = project_root / VERSION_FILE

    # ------------------------------------------------------------------ 查询

    def current_version(self) -> UpstreamState:
        return UpstreamState.load(self._version_path)

    def _upstream_version(self) -> str:
        """取上游版本号：git 用短 commit，本地目录用 mtime 摘要。"""
        repo = Path(self._settings.repo) if self._settings.repo else None
        if repo is not None and repo.is_dir():
            return str(int(repo.stat().st_mtime))

        if not self._settings.repo:
            raise ToolError(
                ErrorCode.UPSTREAM_UPDATE_FAILED,
                "未配置 upstream.repo，无法检查上游版本",
                detail={},
            )

        result = subprocess.run(
            ["git", "ls-remote", self._settings.repo, self._settings.ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise ToolError(
                ErrorCode.UPSTREAM_UPDATE_FAILED,
                "读取上游版本失败",
                detail={"repo": self._settings.repo, "stderr": result.stderr.strip()[:300]},
            )
        first = (result.stdout or "").split()
        return first[0][:12] if first else ""

    # ------------------------------------------------------------------ 主流程

    def update(self, *, check_only: bool = False, force: bool = False) -> UpdateUpstreamData:
        old = self.current_version()
        new_version = self._upstream_version()

        if check_only:
            return UpdateUpstreamData(
                updated=False,
                from_version=old.version,
                to_version=new_version,
                notes=["仅检查，未做任何修改"],
            )

        if not force and new_version and new_version == old.version:
            return UpdateUpstreamData(
                updated=False,
                from_version=old.version,
                to_version=new_version,
                notes=["已是最新版本"],
            )

        backup_path = self._backup(old_version=old.version)
        _log.info("上游更新开始：%s → %s，备份=%s", old.version or "-", new_version, backup_path)

        try:
            changed = self._sync()
            compatible, problems = self._verify()
            if not compatible:
                raise ToolError(
                    ErrorCode.UPSTREAM_INCOMPATIBLE,
                    "上游更新后兼容性校验失败：" + "；".join(problems),
                    detail={"changed_files": changed, "problems": problems},
                )
        except ToolError as exc:
            rolled = self._rollback(backup_path)
            _log.error("上游更新失败并%s回滚：%s", "" if rolled else "未能", exc.message)
            raise ToolError(
                exc.code,
                exc.message + ("（已回滚）" if rolled else "（回滚失败，请手工恢复备份）"),
                detail={**exc.detail, "backup_path": str(backup_path), "rolled_back": rolled},
            ) from exc

        new_state = UpstreamState(
            version=new_version,
            ref=self._settings.ref,
            repo=self._settings.repo,
            synced_at=now_local().isoformat(),
            files=changed,
        )
        new_state.save(self._version_path)
        self._prune_backups()
        _log.info("上游更新完成：%d 个文件变更", len(changed))

        return UpdateUpstreamData(
            updated=True,
            from_version=old.version,
            to_version=new_version,
            backup_path=str(backup_path),
            changed_files=changed,
            compatible=True,
            rolled_back=False,
            notes=["更新成功，兼容性校验通过"],
        )

    # ------------------------------------------------------------------ 步骤

    def _backup(self, *, old_version: str) -> Path:
        """备份当前 vendor 目录与版本文件。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._backup_root / f"upstream-{stamp}"
        target.mkdir(parents=True, exist_ok=True)

        if self._vendor.is_dir():
            shutil.copytree(self._vendor, target / VENDOR_DIR, dirs_exist_ok=True)
        if self._version_path.is_file():
            shutil.copy2(self._version_path, target / VERSION_FILE)

        (target / "backup.meta.json").write_text(
            json.dumps(
                {"old_version": old_version, "created_at": now_local().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target

    def _sync(self) -> list[str]:
        """把上游内容同步进 vendor 目录，返回变更文件名列表。"""
        repo_setting = self._settings.repo
        if not repo_setting:
            raise ToolError(
                ErrorCode.UPSTREAM_UPDATE_FAILED,
                "未配置 upstream.repo",
                detail={},
            )

        repo_path = Path(repo_setting)
        if repo_path.is_dir():
            return self._sync_from_dir(repo_path)
        return self._sync_from_git(repo_setting)

    def _sync_from_dir(self, source: Path) -> list[str]:
        """本地目录同步（开发机自测用）。"""
        changed: list[str] = []
        sub_paths = self._settings.paths or ["."]
        for relative in sub_paths:
            src = source / relative if relative != "." else source
            if not src.exists():
                continue
            dst = self._vendor / relative if relative != "." else self._vendor
            changed.extend(_copy_tree(src, dst))
        if not changed:
            changed = _list_files(self._vendor)
        return changed

    def _sync_from_git(self, repo: str) -> list[str]:
        """从 git 仓库浅拉取到临时目录，再拷进 vendor。"""
        tmp_root = self._backup_root / ".upstream-tmp"
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.parent.mkdir(parents=True, exist_ok=True)

        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", self._settings.ref, repo, str(tmp_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        if clone.returncode != 0:
            raise ToolError(
                ErrorCode.UPSTREAM_UPDATE_FAILED,
                "克隆上游仓库失败",
                detail={"repo": repo, "stderr": clone.stderr.strip()[:300]},
            )

        try:
            changed: list[str] = []
            for relative in self._settings.paths or ["."]:
                src = tmp_root / relative if relative != "." else tmp_root
                if not src.exists():
                    continue
                dst = self._vendor / relative if relative != "." else self._vendor
                changed.extend(_copy_tree(src, dst))
            return changed or _list_files(self._vendor)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def _verify(self) -> tuple[bool, list[str]]:
        """兼容性校验：关键文件存在 + 版本文件可解析。"""
        if not self._settings.verify_after_update:
            return True, []

        problems: list[str] = []
        for required in REQUIRED_FILES:
            if not (self._vendor / required).is_file():
                problems.append(f"缺少关键文件 {required}")

        # tools schema 必须能解析成 JSON（防止上游破坏性改动直接把服务带崩）。
        schema_path = self._vendor / "schemas" / "tools.json"
        if schema_path.is_file():
            try:
                json.loads(schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                problems.append(f"schemas/tools.json 不是合法 JSON：{exc}")

        return not problems, problems

    def _rollback(self, backup_path: Path) -> bool:
        """用备份覆盖回去；返回是否成功。"""
        try:
            backup_vendor = backup_path / VENDOR_DIR
            if backup_vendor.is_dir():
                if self._vendor.exists():
                    shutil.rmtree(self._vendor, ignore_errors=True)
                shutil.copytree(backup_vendor, self._vendor)
            backup_version = backup_path / VERSION_FILE
            if backup_version.is_file():
                shutil.copy2(backup_version, self._version_path)
            _log.warning("已回滚到备份：%s", backup_path)
            return True
        except OSError as exc:  # pragma: no cover - 磁盘问题
            _log.error("回滚失败：%s", exc)
            return False

    def _prune_backups(self) -> None:
        """只保留最近 N 份备份。"""
        if not self._backup_root.is_dir():
            return
        candidates = sorted(
            (p for p in self._backup_root.iterdir() if p.is_dir() and p.name.startswith("upstream-")),
            key=lambda p: p.name,
            reverse=True,
        )
        for stale in candidates[self._settings.keep_backups :]:
            shutil.rmtree(stale, ignore_errors=True)


def _copy_tree(source: Path, destination: Path) -> list[str]:
    """递归拷贝并返回相对目标根的变更文件列表。"""
    changed: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)

    for item in source.rglob("*"):
        if item.is_dir() or ".git" in item.parts:
            continue
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            same_size = target.is_file() and target.stat().st_size == item.stat().st_size
            if same_size and target.read_bytes() == item.read_bytes():
                continue
        except OSError:
            pass
        shutil.copy2(item, target)
        changed.append(relative.as_posix())
    return changed


def _list_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


__all__ = ["REQUIRED_FILES", "VENDOR_DIR", "VERSION_FILE", "UpstreamState", "UpstreamUpdater"]
