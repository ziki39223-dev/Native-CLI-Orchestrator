"""契约测试：六个工具的入参/出参结构，以及工具服务层的错误映射。

要求：任何字段改动都算破坏契约，必须同步 docs/06。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from native_cli_orchestrator.config.accounts import parse_accounts
from native_cli_orchestrator.config.settings import AppSettings
from native_cli_orchestrator.contracts.constants import GROUPS, AccountState, Mode, TaskState
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import (
    GetPoolStatusRequest,
    GetTaskResultRequest,
    ReloadConfigRequest,
    SubmitTaskRequest,
    SwitchAccountRequest,
    TaskManifest,
    UpdateUpstreamRequest,
)
from native_cli_orchestrator.pools.scheduler import Scheduler
from native_cli_orchestrator.runners.mock import MockRunner
from native_cli_orchestrator.tools.service import ToolService

ENVELOPE_KEYS = {
    "ok",
    "error_code",
    "message",
    "backend",
    "correlation_id",
    "elapsed_ms",
    "error",
    "data",
}


@pytest.fixture
def service(settings_mock: AppSettings) -> ToolService:
    accounts = parse_accounts(
        {
            "groups": {
                "trae_group": {"accounts": [{"label": "trae-a"}, {"label": "trae-b"}]},
                "qoder_group": {"accounts": [{"label": "qoder-a"}]},
                "workbuddy_group": {"accounts": [{"label": "wb-a"}]},
            }
        }
    )
    scheduler = Scheduler(
        accounts=accounts,
        runner=MockRunner(plan={}),
        workspace_root=settings_mock.workspace_root,
        default_timeout_s=5.0,
        group_cooldown_s=0.0,
    )
    return ToolService(settings=settings_mock, accounts=accounts, scheduler=scheduler)


def _manifest() -> TaskManifest:
    return TaskManifest(goal="契约测试", prompt="请输出一段文字", acceptance=["可读"])


# --------------------------------------------------------------------------- #
# 公共信封
# --------------------------------------------------------------------------- #


def test_envelope_shape(service: ToolService) -> None:
    payload = service.get_pool_status().model_dump(mode="json")
    assert set(payload) == ENVELOPE_KEYS
    assert payload["ok"] is True
    assert payload["error_code"] == "OK"
    assert payload["backend"] == Mode.MOCK.value
    assert payload["correlation_id"]
    assert payload["error"] is None


def test_correlation_id_unique(service: ToolService) -> None:
    first = service.get_pool_status().correlation_id
    second = service.get_pool_status().correlation_id
    assert first != second


# --------------------------------------------------------------------------- #
# 1. submit_task
# --------------------------------------------------------------------------- #


def test_submit_task_contract(service: ToolService) -> None:
    payload = service.submit_task(
        SubmitTaskRequest(group="trae_group", task_manifest=_manifest(), wait=True)
    ).model_dump(mode="json")

    assert payload["ok"] is True
    data = payload["data"]
    assert set(data) >= {
        "task_id",
        "group",
        "state",
        "account_index",
        "account_label",
        "workspace",
        "submitted_at",
        "waited",
    }
    assert data["state"] == TaskState.SUCCEEDED.value
    assert data["waited"] is True
    assert Path(data["workspace"]).is_dir()


def test_submit_task_unknown_group(service: ToolService) -> None:
    # 绕过 Literal 校验，验证调度层 → 工具层的错误映射
    envelope = service._run(
        "submit_task",
        {"group": "nope"},
        lambda: service.scheduler.submit(group="nope", manifest=_manifest(), wait=True),
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.GROUP_NOT_FOUND
    assert envelope.data is None


def test_submit_task_invalid_manifest(service: ToolService) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TaskManifest(goal="   ", prompt="x")
    payload = service.invalid("submit_task", excinfo.value)
    assert payload["ok"] is False
    assert payload["error_code"] == ErrorCode.INVALID_ARGUMENT.value
    assert payload["error"]["detail"]["fields"]


def test_submit_task_prompt_too_long(service: ToolService) -> None:
    with pytest.raises(ValidationError):
        TaskManifest(goal="g", prompt="x" * 200_001)


# --------------------------------------------------------------------------- #
# 2. get_task_result
# --------------------------------------------------------------------------- #


def test_get_task_result_contract(service: ToolService) -> None:
    submitted = service.submit_task(
        SubmitTaskRequest(group="qoder_group", task_manifest=_manifest(), wait=True)
    )
    assert submitted.data is not None

    payload = service.get_task_result(
        GetTaskResultRequest(task_id=submitted.data.task_id)
    ).model_dump(mode="json")

    data = payload["data"]
    assert data["succeeded"] is True
    assert data["state"] == TaskState.SUCCEEDED.value
    assert isinstance(data["artifacts"], list)
    assert data["artifacts"], "成功任务应有产物"
    assert {"path", "size_bytes", "sha256"} <= set(data["artifacts"][0])


def test_get_task_result_not_found(service: ToolService) -> None:
    payload = service.get_task_result(
        GetTaskResultRequest(task_id="t-missing")
    ).model_dump(mode="json")
    assert payload["ok"] is False
    assert payload["error_code"] == ErrorCode.TASK_NOT_FOUND.value


def test_get_task_result_queued_is_ok(settings_mock: AppSettings) -> None:
    """queued/running 是正常状态，信封必须 ok=true（不是错误）。"""
    accounts = parse_accounts(
        {
            "groups": {
                "trae_group": {"accounts": [{"label": "trae-a"}]},
                "qoder_group": {"accounts": [{"label": "qoder-a"}]},
                "workbuddy_group": {"accounts": [{"label": "wb-a"}]},
            }
        }
    )
    scheduler = Scheduler(
        accounts=accounts,
        runner=MockRunner(plan={}, latency_s=0.5),
        workspace_root=settings_mock.workspace_root,
        default_timeout_s=5.0,
    )
    service = ToolService(settings=settings_mock, accounts=accounts, scheduler=scheduler)

    submitted = service.submit_task(
        SubmitTaskRequest(group="trae_group", task_manifest=_manifest(), wait=False)
    )
    assert submitted.data is not None
    payload = service.get_task_result(
        GetTaskResultRequest(task_id=submitted.data.task_id, wait_s=0.0)
    ).model_dump(mode="json")

    assert payload["ok"] is True
    assert payload["data"]["state"] in {TaskState.QUEUED.value, TaskState.RUNNING.value}
    assert payload["data"]["succeeded"] is False


# --------------------------------------------------------------------------- #
# 3. get_pool_status
# --------------------------------------------------------------------------- #


def test_pool_status_contract(service: ToolService) -> None:
    payload = service.get_pool_status().model_dump(mode="json")
    data = payload["data"]

    assert data["healthy"] is True
    assert len(data["groups"]) == len(GROUPS)
    group = data["groups"][0]
    assert set(group) >= {"group", "active_index", "queued", "running", "accounts"}

    account = group["accounts"][0]
    assert set(account) >= {
        "index",
        "label",
        "state",
        "active",
        "busy",
        "consecutive_failures",
    }
    # 单实例不变量：同一组只有一个 active
    assert sum(1 for a in group["accounts"] if a["active"]) <= 1


def test_pool_status_single_group(service: ToolService) -> None:
    payload = service.get_pool_status(GetPoolStatusRequest(group="qoder_group"))
    assert payload.data is not None
    assert [g.group for g in payload.data.groups] == ["qoder_group"]


def test_pool_status_never_leaks_credentials(service: ToolService) -> None:
    """出参里只能有 label，不能有 env 里的实际值。"""
    text = service.get_pool_status().model_dump_json()
    assert "alpha" not in text
    assert "TRAE_TOKEN" not in text


# --------------------------------------------------------------------------- #
# 4. switch_account
# --------------------------------------------------------------------------- #


def test_switch_account_contract(service: ToolService) -> None:
    payload = service.switch_account(SwitchAccountRequest(group="trae_group")).model_dump(
        mode="json"
    )
    data = payload["data"]
    assert data["switched"] is True
    assert data["previous_index"] == 0
    assert data["active_index"] == 1
    assert data["active_label"] == "trae-b"
    assert data["previous_state"] == AccountState.DISABLED.value


def test_switch_account_out_of_range(service: ToolService) -> None:
    payload = service.switch_account(
        SwitchAccountRequest(group="qoder_group", target_index=5)
    ).model_dump(mode="json")
    assert payload["ok"] is False
    assert payload["error_code"] == ErrorCode.INVALID_ARGUMENT.value


# --------------------------------------------------------------------------- #
# 5. reload_config
# --------------------------------------------------------------------------- #


def test_reload_config_contract(service: ToolService, accounts_file: Path) -> None:
    payload = service.reload_config(ReloadConfigRequest(path=str(accounts_file))).model_dump(
        mode="json"
    )
    data = payload["data"]
    assert data["loaded"] is True
    assert set(data["groups"]) == set(GROUPS)
    assert data["accounts_total"] == 7  # 3 + 2 + 2
    assert data["config_version"] == "test-1.0"


def test_reload_config_bad_path(service: ToolService) -> None:
    payload = service.reload_config(ReloadConfigRequest(path="不存在.yml")).model_dump(mode="json")
    assert payload["ok"] is False
    assert payload["error_code"] == ErrorCode.CONFIG_INVALID.value


# --------------------------------------------------------------------------- #
# 6. update_upstream
# --------------------------------------------------------------------------- #


def test_update_upstream_disabled_reports_error(service: ToolService) -> None:
    payload = service.update_upstream(UpdateUpstreamRequest()).model_dump(mode="json")
    assert payload["ok"] is False
    assert payload["error_code"] == ErrorCode.UPSTREAM_UPDATE_FAILED.value


def test_update_upstream_contract(tmp_path: Path, settings_mock: AppSettings) -> None:
    """配置了更新器时的成功出参结构。"""
    import json

    from native_cli_orchestrator.config.settings import UpstreamSettings
    from native_cli_orchestrator.upstream.updater import UpstreamUpdater

    repo = tmp_path / "repo" / "schemas"
    repo.mkdir(parents=True)
    (repo / "tools.json").write_text(json.dumps({"tools": []}), encoding="utf-8")

    accounts = parse_accounts(
        {
            "groups": {
                "trae_group": {"accounts": [{"label": "trae-a"}]},
                "qoder_group": {"accounts": [{"label": "qoder-a"}]},
                "workbuddy_group": {"accounts": [{"label": "wb-a"}]},
            }
        }
    )
    scheduler = Scheduler(
        accounts=accounts,
        runner=MockRunner(),
        workspace_root=settings_mock.workspace_root,
    )
    updater = UpstreamUpdater(
        settings=UpstreamSettings(enabled=True, repo=str(tmp_path / "repo")),
        project_root=tmp_path,
        backup_root=tmp_path / "backups",
    )
    service = ToolService(
        settings=settings_mock.model_copy(
            update={"upstream": UpstreamSettings(enabled=True, repo=str(tmp_path / "repo"))}
        ),
        accounts=accounts,
        scheduler=scheduler,
        updater=updater,
    )

    payload = service.update_upstream(UpdateUpstreamRequest()).model_dump(mode="json")
    data = payload["data"]
    assert set(data) == {
        "updated",
        "from_version",
        "to_version",
        "backup_path",
        "changed_files",
        "compatible",
        "rolled_back",
        "notes",
    }
    assert data["updated"] is True
    assert data["compatible"] is True
    assert data["rolled_back"] is False


# --------------------------------------------------------------------------- #
# 内部错误兜底
# --------------------------------------------------------------------------- #


def test_internal_error_wrapped(service: ToolService) -> None:
    def boom() -> None:
        raise RuntimeError("模拟未预期错误")

    envelope = service._run("boom", {}, boom)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR
    assert envelope.data is None
    assert envelope.error is not None
    assert "RuntimeError" in envelope.error.message


def test_tool_error_mapped_to_code(service: ToolService) -> None:
    def fail() -> None:
        raise ToolError(ErrorCode.POOL_EXHAUSTED, "全组无可用账号", detail={"group": "trae_group"})

    envelope = service._run("x", {}, fail)
    assert envelope.error_code is ErrorCode.POOL_EXHAUSTED
    assert envelope.error is not None
    assert envelope.error.detail["group"] == "trae_group"


__all__ = ["Any"]
