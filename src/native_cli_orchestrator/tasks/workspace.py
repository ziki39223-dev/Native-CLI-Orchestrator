"""任务队列：inbox + manifest + .DONE 产物协议。

目录结构（每个任务一个工作目录）::

    workspace/<task_id>/
      manifest.json     提交时的任务清单（不可变）
      .task.json        调度器内部状态（重启用）
      prompt.md         最终下发给 CLI 的完整指令（含验收标准拼接）
      inbox/            CLI 从 inbox 读取命令文件；调度器写入 request.json
      outbox/           保留：CLI 主动产出
      stdout.log        CLI 标准输出（脱敏后）
      stderr.log        CLI 标准错误（脱敏后）
      artifacts/        CLI 产出的文件（产物）
      result.json       调度器写回的结构化结果
      .DONE             完成标记（出现过即视为完成）
      .FAILED           失败标记（与 .DONE 互斥）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.text import now_local, redact
from ..contracts.constants import (
    DONE_MARKER,
    FAIL_MARKER,
    MANIFEST_NAME,
    RESULT_NAME,
    STATE_NAME,
    TaskState,
)
from ..contracts.errors import ErrorCode, ToolError
from ..contracts.tools import TaskArtifact, TaskManifest
from ..observability.logging import get_logger

_log = get_logger("dao.scheduler")

INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
ARTIFACT_DIR = "artifacts"
REQUEST_NAME = "request.json"
PROMPT_NAME = "prompt.md"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"


@dataclass(slots=True)
class TaskWorkspace:
    """一个任务的落盘工作区。"""

    task_id: str
    group: str
    root: Path

    # ------------------------------------------------------------------ 路径

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def state_path(self) -> Path:
        return self.root / STATE_NAME

    @property
    def result_path(self) -> Path:
        return self.root / RESULT_NAME

    @property
    def prompt_path(self) -> Path:
        return self.root / PROMPT_NAME

    @property
    def done_path(self) -> Path:
        return self.root / DONE_MARKER

    @property
    def failed_path(self) -> Path:
        return self.root / FAIL_MARKER

    @property
    def inbox_dir(self) -> Path:
        return self.root / INBOX_DIR

    @property
    def outbox_dir(self) -> Path:
        return self.root / OUTBOX_DIR

    @property
    def artifact_dir(self) -> Path:
        return self.root / ARTIFACT_DIR

    @property
    def stdout_path(self) -> Path:
        return self.root / STDOUT_NAME

    @property
    def stderr_path(self) -> Path:
        return self.root / STDERR_NAME

    # ------------------------------------------------------------------ 创建

    @classmethod
    def create(cls, workspace_root: Path, task_id: str, group: str) -> TaskWorkspace:
        """建目录骨架；已存在则复用（重试同一任务时）。"""
        root = workspace_root / task_id
        for directory in (root, root / INBOX_DIR, root / OUTBOX_DIR, root / ARTIFACT_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(task_id=task_id, group=group, root=root)

    # ------------------------------------------------------------------ 写入

    def write_manifest(self, manifest: TaskManifest) -> None:
        self._write_json(self.manifest_path, manifest.model_dump(mode="json"))

    def read_manifest(self) -> TaskManifest:
        if not self.manifest_path.is_file():
            raise ToolError(
                ErrorCode.TASK_NOT_FOUND,
                f"任务 {self.task_id} 缺少 manifest.json",
                detail={"task_id": self.task_id, "path": str(self.manifest_path)},
            )

        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 文件在但读不出来：属配置/数据损坏，不是"任务不存在"。
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"任务 {self.task_id} 的 manifest.json 无法解析",
                detail={"task_id": self.task_id, "reason": str(exc)},
            ) from exc

        try:
            return TaskManifest.model_validate(raw)
        except Exception as exc:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"任务 {self.task_id} 的 manifest.json 非法",
                detail={"task_id": self.task_id, "reason": str(exc)},
            ) from exc

    def write_prompt(self, manifest: TaskManifest) -> Path:
        """把清单渲染成给 CLI 的完整指令（含验收标准）。"""
        lines = [manifest.prompt.strip(), "", "## 验收标准"]
        if manifest.acceptance:
            lines.extend(f"- {item}" for item in manifest.acceptance)
        else:
            lines.append("- （未提供，按目标自检）")
        if manifest.artifacts:
            lines.extend(["", "## 期望产物"])
            lines.extend(f"- {item}" for item in manifest.artifacts)
        lines.extend(
            [
                "",
                "## 运行约定",
                f"- 工作目录：{self.root}",
                f"- 命令请求文件：{self.inbox_dir / REQUEST_NAME}",
                f"- 产物请写入：{self.artifact_dir}",
                f"- 完成后写入标记文件：{self.done_path}",
                "- 若判定无法完成，请写入失败标记并说明原因：" + str(self.failed_path),
            ]
        )
        self.prompt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.prompt_path

    def write_request(self, manifest: TaskManifest, *, account_label: str, attempt: int) -> Path:
        """写 CLI 的 inbox 请求文件（协议核心）。"""
        payload = {
            "task_id": self.task_id,
            "group": self.group,
            "account_label": account_label,
            "attempt": attempt,
            "goal": manifest.goal,
            "prompt": manifest.prompt,
            "acceptance": manifest.acceptance,
            "expected_artifacts": manifest.artifacts,
            "model": manifest.model,
            "metadata": manifest.metadata,
            "workspace": str(self.root),
            "artifact_dir": str(self.artifact_dir),
            "done_marker": str(self.done_path),
            "created_at": now_local().isoformat(),
        }
        path = self.inbox_dir / REQUEST_NAME
        self._write_json(path, payload)
        return path

    def append_output(self, *, stdout: str = "", stderr: str = "") -> None:
        """追加 CLI 输出（**脱敏后**落盘，避免凭据进日志文件）。"""
        if stdout:
            with self.stdout_path.open("a", encoding="utf-8") as handle:
                handle.write(redact(stdout))
        if stderr:
            with self.stderr_path.open("a", encoding="utf-8") as handle:
                handle.write(redact(stderr))

    def write_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_path, state)

    def read_state(self) -> dict[str, Any]:
        return self._read_json(self.state_path) or {}

    def write_result(self, payload: dict[str, Any]) -> None:
        self._write_json(self.result_path, payload)

    def mark_done(self, *, summary: str = "") -> None:
        """写完成标记；先清失败标记，保证互斥。"""
        self.failed_path.unlink(missing_ok=True)
        self.done_path.write_text(summary or now_local().isoformat(), encoding="utf-8")

    def mark_failed(self, *, reason: str = "") -> None:
        self.done_path.unlink(missing_ok=True)
        self.failed_path.write_text(reason or now_local().isoformat(), encoding="utf-8")

    # ------------------------------------------------------------------ 读取

    def is_done(self) -> bool:
        return self.done_path.is_file()

    def is_failed(self) -> bool:
        return self.failed_path.is_file()

    def failure_reason(self) -> str:
        try:
            return self.failed_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def read_tail(self, *, lines: int = 200) -> str:
        """读 stdout 尾部若干行，用于故障识别与结果摘要。"""
        return _tail(self.stdout_path, lines) + _tail(self.stderr_path, lines)

    def collect_artifacts(self, *, expected: list[str] | None = None) -> list[TaskArtifact]:
        """收集产物：``artifacts/`` 下全部文件，优先列出期望清单。

        同时把任务根下（非协议文件）的产出也纳入——CLI 常常直接写在工作目录。
        """
        candidates: list[Path] = []
        if self.artifact_dir.is_dir():
            candidates.extend(sorted(p for p in self.artifact_dir.rglob("*") if p.is_file()))

        protocol_names = {
            MANIFEST_NAME,
            STATE_NAME,
            RESULT_NAME,
            PROMPT_NAME,
            STDOUT_NAME,
            STDERR_NAME,
            REQUEST_NAME,
            DONE_MARKER,
            FAIL_MARKER,
        }
        for path in sorted(self.root.iterdir()):
            if path.is_file() and path.name not in protocol_names:
                candidates.append(path)

        artifacts: list[TaskArtifact] = []
        seen: set[str] = set()
        for path in candidates:
            relative = path.relative_to(self.root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            artifacts.append(
                TaskArtifact(
                    path=relative,
                    size_bytes=path.stat().st_size,
                    sha256=_sha256(path),
                )
            )

        # 期望产物即使没生成也列出来，便于 DSH 判断"缺了什么"。
        for item in expected or []:
            normalized = item.replace("\\", "/").lstrip("./")
            if normalized not in seen:
                artifacts.append(TaskArtifact(path=normalized, size_bytes=0, sha256=""))
        return artifacts

    def read_result(self) -> dict[str, Any]:
        return self._read_json(self.result_path) or {}

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        """原子写：先写唯一临时文件再替换。

        临时文件名必须唯一（进程 + 线程 + 随机），否则两个线程写同一路径时会互删临时文件，
        在 Windows 上报 FileNotFoundError。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------- #
# 任务索引：把 task_id 映射到工作区与最终状态
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TaskRecord:
    """调度器里的一次任务记录。"""

    task_id: str
    group: str
    state: TaskState
    account_index: int
    account_label: str
    workspace: TaskWorkspace
    submitted_at: str
    updated_at: str
    account_attempts: int = 1
    fail_reason: str = ""
    fault_kind: str = ""
    #: 环境级错误码（CLI_NOT_FOUND / DEPENDENCY_MISSING 等）；非空表示"不是任务的锅"。
    error_code: str = ""
    exit_code: int | None = None
    summary: str = ""
    elapsed_s: float = 0.0
    #: API 分组专用：token 用量与估算花费。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_yuan: float = 0.0
    truncated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "group": self.group,
            "state": self.state.value,
            "account_index": self.account_index,
            "account_label": self.account_label,
            "workspace": str(self.workspace.root),
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "account_attempts": self.account_attempts,
            "fail_reason": self.fail_reason,
            "fault_kind": self.fault_kind,
            "error_code": self.error_code,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "elapsed_s": self.elapsed_s,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_yuan": self.cost_yuan,
            "truncated": self.truncated,
        }

    @classmethod
    def from_json(
        cls, payload: dict[str, Any], *, workspace_root: Path
    ) -> TaskRecord | None:
        try:
            return cls(
                task_id=str(payload["task_id"]),
                group=str(payload["group"]),
                state=TaskState(payload["state"]),
                account_index=int(payload.get("account_index", -1)),
                account_label=str(payload.get("account_label", "")),
                workspace=TaskWorkspace(
                    task_id=str(payload["task_id"]),
                    group=str(payload["group"]),
                    root=workspace_root / str(payload["task_id"]),
                ),
                submitted_at=str(payload.get("submitted_at", "")),
                updated_at=str(payload.get("updated_at", "")),
                account_attempts=int(payload.get("account_attempts", 1)),
                fail_reason=str(payload.get("fail_reason", "")),
                fault_kind=str(payload.get("fault_kind", "")),
                error_code=str(payload.get("error_code", "")),
                exit_code=payload.get("exit_code"),
                summary=str(payload.get("summary", "")),
                elapsed_s=float(payload.get("elapsed_s", 0.0)),
                prompt_tokens=int(payload.get("prompt_tokens", 0)),
                completion_tokens=int(payload.get("completion_tokens", 0)),
                cost_yuan=float(payload.get("cost_yuan", 0.0)),
                truncated=bool(payload.get("truncated", False)),
            )
        except (KeyError, ValueError):
            return None


def _tail(path: Path, lines: int) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if lines <= 0:
        return content
    return "\n".join(content.splitlines()[-lines:])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


__all__ = [
    "ARTIFACT_DIR",
    "INBOX_DIR",
    "OUTBOX_DIR",
    "PROMPT_NAME",
    "REQUEST_NAME",
    "STDERR_NAME",
    "STDOUT_NAME",
    "TaskRecord",
    "TaskWorkspace",
]
