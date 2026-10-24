"""任务工作区协议测试：manifest / inbox / .DONE / 产物 / 脱敏。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import TaskManifest
from native_cli_orchestrator.tasks.workspace import TaskWorkspace


def _ws(tmp_path: Path, task_id: str = "t-trage-001") -> TaskWorkspace:
    return TaskWorkspace.create(tmp_path, task_id, "trae_group")


def _manifest(**overrides: object) -> TaskManifest:
    payload = {"goal": "写开题报告", "prompt": "请写一份开题报告", "acceptance": ["含研究背景"]}
    payload.update(overrides)
    return TaskManifest.model_validate(payload)


# --------------------------------------------------------------------------- #
# 目录与清单
# --------------------------------------------------------------------------- #


def test_create_makes_all_directories(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    assert workspace.root.is_dir()
    assert workspace.inbox_dir.is_dir()
    assert workspace.outbox_dir.is_dir()
    assert workspace.artifact_dir.is_dir()


def test_manifest_roundtrip(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    manifest = _manifest()
    workspace.write_manifest(manifest)

    restored = workspace.read_manifest()
    assert restored.goal == manifest.goal
    assert restored.acceptance == manifest.acceptance


def test_missing_manifest_raises(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        workspace.read_manifest()
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_corrupt_manifest_raises(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    workspace.manifest_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ToolError) as excinfo:
        workspace.read_manifest()
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_prompt_includes_acceptance_and_conventions(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    manifest = _manifest(artifacts=["artifacts/report.md"])
    path = workspace.write_prompt(manifest)
    text = path.read_text(encoding="utf-8")

    assert manifest.prompt in text
    assert "## 验收标准" in text
    assert "- 含研究背景" in text
    assert "## 期望产物" in text
    assert ".DONE" in text, "必须告诉 CLI 完成标记的路径"


def test_request_file_payload(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    manifest = _manifest()
    workspace.write_manifest(manifest)
    path = workspace.write_request(manifest, account_label="trae-a", attempt=2)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["task_id"] == workspace.task_id
    assert payload["group"] == "trae_group"
    assert payload["account_label"] == "trae-a"
    assert payload["attempt"] == 2
    assert payload["goal"] == manifest.goal
    assert payload["artifact_dir"].endswith("artifacts")


# --------------------------------------------------------------------------- #
# 完成/失败标记
# --------------------------------------------------------------------------- #


def test_done_and_failed_are_mutually_exclusive(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)

    workspace.mark_failed(reason="配额不足")
    assert workspace.is_failed() and not workspace.is_done()

    workspace.mark_done(summary="完成")
    assert workspace.is_done() and not workspace.is_failed()

    workspace.mark_failed(reason="又失败")
    assert workspace.is_failed() and not workspace.is_done()


def test_failure_reason_readable(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    workspace.mark_failed(reason="quota_exhausted: 额度不足")
    assert "quota_exhausted" in workspace.failure_reason()


# --------------------------------------------------------------------------- #
# 输出与脱敏
# --------------------------------------------------------------------------- #


def test_output_is_redacted(tmp_path: Path) -> None:
    """凭据绝不能进 stdout.log / stderr.log。"""
    workspace = _ws(tmp_path)
    workspace.append_output(
        stdout="Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef\n",
        stderr="api_key=sk-supersecret1234567890\n",
    )

    combined = workspace.stdout_path.read_text(encoding="utf-8") + workspace.stderr_path.read_text(
        encoding="utf-8"
    )
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef" not in combined
    assert "sk-supersecret1234567890" not in combined
    assert "***" in combined


def test_read_tail_merges_streams(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    workspace.append_output(stdout="line-1\nline-2\n")
    workspace.append_output(stderr="err-1\n")

    tail = workspace.read_tail(lines=10)
    assert "line-1" in tail
    assert "err-1" in tail


# --------------------------------------------------------------------------- #
# 产物收集
# --------------------------------------------------------------------------- #


def test_collect_artifacts_from_dir_and_root(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    (workspace.artifact_dir / "report.md").write_text("内容", encoding="utf-8")
    (workspace.root / "outline.md").write_text("大纲", encoding="utf-8")
    # 协议文件不应被当成产物
    workspace.write_state({"task_id": workspace.task_id})
    workspace.mark_done()

    artifacts = workspace.collect_artifacts()
    paths = {item.path for item in artifacts}
    assert "artifacts/report.md" in paths
    assert "outline.md" in paths
    assert ".task.json" not in paths
    assert ".DONE" not in paths
    assert all(item.size_bytes > 0 for item in artifacts if item.sha256)


def test_collect_artifacts_lists_missing_expected(tmp_path: Path) -> None:
    """期望产物没生成时也要列出来（size=0），便于 DSH 判断缺了什么。"""
    workspace = _ws(tmp_path)
    artifacts = workspace.collect_artifacts(expected=["artifacts/missing.md"])
    assert len(artifacts) == 1
    assert artifacts[0].path == "artifacts/missing.md"
    assert artifacts[0].size_bytes == 0
    assert artifacts[0].sha256 == ""


def test_artifact_sha256_stable(tmp_path: Path) -> None:
    workspace = _ws(tmp_path)
    target = workspace.artifact_dir / "a.txt"
    target.write_text("固定内容", encoding="utf-8")

    first = workspace.collect_artifacts(expected=["artifacts/a.txt"])
    second = workspace.collect_artifacts(expected=["artifacts/a.txt"])
    assert first[0].sha256 == second[0].sha256
    assert len(first[0].sha256) == 64


def test_state_write_is_atomic(tmp_path: Path) -> None:
    """写状态用临时文件 + 替换，不留下 .tmp 残骸。"""
    workspace = _ws(tmp_path)
    workspace.write_state({"a": 1})
    workspace.write_state({"a": 2})

    assert workspace.read_state() == {"a": 2}
    assert not list(workspace.root.glob("*.tmp"))


def test_create_is_idempotent(tmp_path: Path) -> None:
    first = TaskWorkspace.create(tmp_path, "t-x", "trae_group")
    first.write_manifest(_manifest())
    second = TaskWorkspace.create(tmp_path, "t-x", "trae_group")
    assert second.read_manifest().goal == "写开题报告"


__all__ = ["pytest"]
