"""分组类型与 API 组：配置校验、分派、跨组共存。

覆盖三层：
1. 配置层：kind=api 的必填项、cli/api 互斥、分组名放开后的校验；
2. 执行器层：CompositeRunner 按 kind 正确分派；
3. 调度层：CLI 组与 API 组同时存在时互不影响，各自遵守单实例。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from native_cli_orchestrator.config.accounts import parse_accounts
from native_cli_orchestrator.config.settings import AppSettings, RunnerSettings, UpstreamSettings
from native_cli_orchestrator.contracts.constants import GroupKind, TaskState
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import SubmitTaskRequest, TaskManifest
from native_cli_orchestrator.pools.scheduler import Scheduler
from native_cli_orchestrator.runners.api import ApiRunner
from native_cli_orchestrator.runners.composite import CompositeRunner
from native_cli_orchestrator.runners.mock import MockRunner
from native_cli_orchestrator.tools.service import ToolService

BASE_GROUPS = {
    "trae_group": {"accounts": [{"label": "trae-a"}, {"label": "trae-b"}]},
    "qoder_group": {"accounts": [{"label": "qoder-a"}]},
    "workbuddy_group": {"accounts": [{"label": "wb-a"}]},
}


def _doc(**extra: object) -> dict:
    groups = json.loads(json.dumps(BASE_GROUPS))
    groups.update(extra)
    return {"groups": groups}


def _api_group(model: str = "model-x", budget: float = 0.0) -> dict:
    return {
        "kind": "api",
        "api": {
            "base_url": "https://api.example.com/v1",
            "model": model,
            "daily_budget": budget,
            "input_price_per_mtok": 1.0,
            "output_price_per_mtok": 1.0,
        },
        "accounts": [{"label": "api-a", "env": {"OPENAI_API_KEY": "env:T_KEY_A"}}],
    }


# --------------------------------------------------------------------------- #
# 1. 配置层
# --------------------------------------------------------------------------- #


def test_api_group_accepted() -> None:
    cfg = parse_accounts(_doc(paid_group=_api_group()))
    assert "paid_group" in cfg.group_names
    assert cfg.api_group_names == ["paid_group"]
    assert cfg.cli_group_names == ["trae_group", "qoder_group", "workbuddy_group"]
    assert cfg.api_spec("paid_group").model == "model-x"
    assert cfg.group("paid_group").kind is GroupKind.API


def test_custom_group_name_any_provider() -> None:
    """任意家 API 都能挂：分组名自定义，只要给了 api 节。"""
    for name in ("deepseek_group", "kimi_group", "glm_group", "openrouter_group", "local_ollama"):
        cfg = parse_accounts(_doc(**{name: _api_group()}))
        assert cfg.group(name).is_api


def test_api_group_requires_api_section() -> None:
    with pytest.raises(ToolError) as excinfo:
        parse_accounts(_doc(bad_group={"kind": "api", "accounts": [{"label": "x"}]}))
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "api 节" in str(excinfo.value.detail)


def test_api_group_rejects_cli_field() -> None:
    bad = _api_group()
    bad["cli"] = ["somecli"]
    with pytest.raises(ToolError) as excinfo:
        parse_accounts(_doc(bad_group=bad))
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_cli_group_rejects_api_field() -> None:
    with pytest.raises(ToolError) as excinfo:
        parse_accounts(
            _doc(trae_group={"cli": ["traecli"], "api": _api_group()["api"], "accounts": [{"label": "t"}]})
        )
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_api_requires_http_base_url() -> None:
    bad = _api_group()
    bad["api"]["base_url"] = "api.example.com"
    with pytest.raises(ToolError) as excinfo:
        parse_accounts(_doc(bad_group=bad))
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_group_name_pattern_enforced() -> None:
    with pytest.raises(ToolError):
        parse_accounts(_doc(**{"bad name!": _api_group()}))


def test_builtin_cli_groups_still_required() -> None:
    """只配 API 分组不够 —— 必须保留三家（可 disabled），避免误解。"""
    with pytest.raises(ToolError) as excinfo:
        parse_accounts({"groups": {"only_api": _api_group()}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_command_for_rejects_api_group() -> None:
    cfg = parse_accounts(_doc(paid_group=_api_group()))
    with pytest.raises(ToolError) as excinfo:
        cfg.command_for("paid_group")
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


# --------------------------------------------------------------------------- #
# 2. CompositeRunner 分派
# --------------------------------------------------------------------------- #


class _Recorder(MockRunner):
    """记录调用并立即成功，用来断言"到底谁被调用了"。"""

    def __init__(self, tag: str) -> None:
        super().__init__(plan={})
        self.tag = tag

    def run(self, context):  # type: ignore[no-untyped-def]
        self.calls.append((self.tag, context.group, context.account_index, context.attempt))
        from native_cli_orchestrator.runners.base import RunResult

        return RunResult(succeeded=True, summary=f"{self.tag} did it")


def test_composite_dispatches_by_kind() -> None:
    cli, api = _Recorder("cli"), _Recorder("api")
    runner = CompositeRunner(cli=cli, api=api)
    runner.bind_groups({"trae_group": GroupKind.CLI, "paid_group": GroupKind.API})

    assert runner._kind_for("trae_group") is GroupKind.CLI
    assert runner._kind_for("paid_group") is GroupKind.API
    assert cli.calls == [] and api.calls == []


def test_composite_unknown_group_raises() -> None:
    runner = CompositeRunner(cli=_Recorder("cli"), api=_Recorder("api"))
    runner.bind_groups({"trae_group": GroupKind.CLI})
    with pytest.raises(ToolError) as excinfo:
        runner._kind_for("nope")
    assert excinfo.value.code is ErrorCode.GROUP_NOT_FOUND


# --------------------------------------------------------------------------- #
# 3. 调度层：CLI 组与 API 组共存
# --------------------------------------------------------------------------- #


def _service(tmp_path: Path, doc: dict) -> tuple[ToolService, CompositeRunner]:
    accounts = parse_accounts(doc)
    cli_runner = MockRunner(plan={})
    api_runner = ApiRunner(budget_path=tmp_path / ".budget.json")
    runner = CompositeRunner(cli=cli_runner, api=api_runner)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "ws",
        max_account_attempts=3,
        default_timeout_s=10.0,
        group_cooldown_s=0.0,
        switch_cooldown_s=0.0,
    )
    settings = AppSettings(
        mode="mock",  # type: ignore[arg-type]
        accounts_path=tmp_path / "accounts.yml",
        workspace_root=tmp_path / "ws",
        log_dir=None,
        runner=RunnerSettings(group_cooldown_s=0.0, switch_cooldown_s=0.0),
        upstream=UpstreamSettings(enabled=False),
    )
    return ToolService(settings=settings, accounts=accounts, scheduler=scheduler), runner


def _mock_openai(monkeypatch: pytest.MonkeyPatch, *, content: str = "回答") -> None:
    real_client = httpx2.AsyncClient

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx2.MockTransport(
            lambda req: httpx2.Response(
                200,
                json={
                    "model": "model-x",
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", factory)


def test_cli_and_api_groups_coexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T_KEY_A", "sk-a")
    _mock_openai(monkeypatch)
    service, runner = _service(tmp_path, _doc(paid_group=_api_group()))

    cli_task = service.submit_task(
        SubmitTaskRequest(group="trae_group", task_manifest=TaskManifest(goal="g", prompt="p"), wait=True)
    )
    api_task = service.submit_task(
        SubmitTaskRequest(group="paid_group", task_manifest=TaskManifest(goal="g", prompt="p"), wait=True)
    )

    assert cli_task.data.state is TaskState.SUCCEEDED
    assert api_task.data.state is TaskState.SUCCEEDED
    assert runner.cli.calls, "CLI 组应走 cli 执行器"
    assert isinstance(runner.cli, MockRunner)


def test_api_group_reports_tokens_and_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T_KEY_A", "sk-a")
    _mock_openai(monkeypatch)
    service, _ = _service(tmp_path, _doc(paid_group=_api_group()))

    submitted = service.submit_task(
        SubmitTaskRequest(group="paid_group", task_manifest=TaskManifest(goal="g", prompt="p"), wait=True)
    )
    from native_cli_orchestrator.contracts.tools import GetTaskResultRequest

    result = service.get_task_result(GetTaskResultRequest(task_id=submitted.data.task_id))

    assert result.data.succeeded is True
    assert result.data.prompt_tokens == 100
    assert result.data.completion_tokens == 50
    assert result.data.cost_yuan == pytest.approx(0.00015)


def test_pool_status_shows_kind_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T_KEY_A", "sk-a")
    _mock_openai(monkeypatch)
    service, _ = _service(tmp_path, _doc(paid_group=_api_group(budget=5.0)))
    service.submit_task(
        SubmitTaskRequest(group="paid_group", task_manifest=TaskManifest(goal="g", prompt="p"), wait=True)
    )

    status = service.get_pool_status()
    assert status.data is not None
    kinds = {g.group: g.kind for g in status.data.groups}
    assert kinds["trae_group"] is GroupKind.CLI
    assert kinds["paid_group"] is GroupKind.API

    api_status = next(g for g in status.data.groups if g.group == "paid_group")
    assert api_status.api_budget["daily_budget"] == 5.0
    assert api_status.api_budget["calls_today"] == 1
    # CLI 组不带预算信息
    cli_status = next(g for g in status.data.groups if g.group == "trae_group")
    assert cli_status.api_budget == {}


def test_api_group_single_instance_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 组同样遵守"同组只一个在线"：两个 key 里只有一个 active。"""
    monkeypatch.setenv("T_KEY_A", "sk-a")
    monkeypatch.setenv("T_KEY_B", "sk-b")
    _mock_openai(monkeypatch)
    group = _api_group()
    group["accounts"] = [
        {"label": "api-a", "env": {"OPENAI_API_KEY": "env:T_KEY_A"}},
        {"label": "api-b", "env": {"OPENAI_API_KEY": "env:T_KEY_B"}},
    ]
    service, _ = _service(tmp_path, _doc(paid_group=group))

    status = service.get_pool_status()
    assert status.data is not None
    api_status = next(g for g in status.data.groups if g.group == "paid_group")
    assert sum(1 for a in api_status.accounts if a.active) == 1
    assert api_status.active_index == 0


def test_reload_config_can_add_api_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """热重载新增 API 组后，分派表也要更新（否则新组跑不起来）。"""
    monkeypatch.setenv("T_KEY_A", "sk-a")
    _mock_openai(monkeypatch)
    service, runner = _service(tmp_path, _doc())

    assert "paid_group" not in runner.kinds  # type: ignore[operator]

    import yaml

    accounts_file = tmp_path / "accounts_new.yml"
    accounts_file.write_text(
        yaml.safe_dump(_doc(paid_group=_api_group()), allow_unicode=True), encoding="utf-8"
    )
    from native_cli_orchestrator.contracts.tools import ReloadConfigRequest

    reloaded = service.reload_config(ReloadConfigRequest(path=str(accounts_file)))
    assert reloaded.ok is True
    assert runner.kinds["paid_group"] is GroupKind.API  # type: ignore[index]

    submitted = service.submit_task(
        SubmitTaskRequest(group="paid_group", task_manifest=TaskManifest(goal="g", prompt="p"), wait=True)
    )
    assert submitted.data.state is TaskState.SUCCEEDED
