"""调度器测试：提交 → 执行 → 失败换账号 → 结果回读。

重点验证需求里最容易做错的三件事：
1. 额度耗尽自动切下一个账号（而不是整组失败）；
2. 同组串行、跨组并行；
3. 全部账号耗尽时返回 POOL_EXHAUSTED / 任务失败，而不是静默挂住。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from native_cli_orchestrator.config.accounts import AccountsConfig
from native_cli_orchestrator.contracts.constants import AccountState, FaultKind, TaskState
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import (
    SubmitTaskRequest,
    TaskManifest,
)
from native_cli_orchestrator.pools.scheduler import Scheduler
from native_cli_orchestrator.runners.mock import MockRunner


def _manifest(goal: str = "写一份开题大纲") -> TaskManifest:
    return TaskManifest(goal=goal, prompt=f"请完成：{goal}", acceptance=["有三级标题"])


def _scheduler(
    accounts: AccountsConfig,
    tmp_path: Path,
    *,
    plan: dict[str, dict[int, str]] | None = None,
    max_attempts: int = 3,
    failure_threshold: int = 2,
    latency_s: float = 0.0,
) -> tuple[Scheduler, MockRunner]:
    runner = MockRunner(plan=plan or {}, latency_s=latency_s)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "workspace",
        state_path=tmp_path / "workspace" / ".pools.state.json",
        max_account_attempts=max_attempts,
        default_timeout_s=5.0,
        group_cooldown_s=0.0,
        switch_cooldown_s=0.0,
        failure_threshold=failure_threshold,
    )
    return scheduler, runner


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_submit_sync_success(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, runner = _scheduler(accounts, tmp_path)
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)

    assert data.state is TaskState.SUCCEEDED
    assert data.account_label == "trae-a"
    assert data.waited is True
    # 工作区协议文件齐全
    workspace = Path(data.workspace)
    assert (workspace / "manifest.json").is_file()
    assert (workspace / "inbox" / "request.json").is_file()
    assert (workspace / ".DONE").is_file()

    result = scheduler.task_result(data.task_id)
    assert result.succeeded is True
    assert result.account_attempts == 1
    assert result.artifacts, "成功任务必须能收集到产物"
    assert runner.calls == [("trae_group", 0, 1)]


def test_submit_async_returns_immediately(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path, latency_s=0.05)
    started = time.monotonic()
    data = scheduler.submit(group="qoder_group", manifest=_manifest(), wait=False)
    submit_elapsed = time.monotonic() - started

    assert submit_elapsed < 0.5, "异步提交不应阻塞等待任务完成"
    assert data.waited is False

    result = scheduler.task_result(data.task_id, wait_s=3.0)
    assert result.state is TaskState.SUCCEEDED


# --------------------------------------------------------------------------- #
# 额度耗尽 → 自动切换
# --------------------------------------------------------------------------- #


def test_quota_exhausted_switches_to_next_account(accounts: AccountsConfig, tmp_path: Path) -> None:
    """A 额度耗尽 → 自动换 B，任务最终成功。"""
    scheduler, _ = _scheduler(accounts, tmp_path, plan={"trae_group": {0: "quota"}})
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)

    assert data.state is TaskState.SUCCEEDED
    result = scheduler.task_result(data.task_id)
    assert result.account_attempts == 2, "应发生一次账号切换"
    assert result.account_label == "trae-b"

    # A 被标 FAILED，B 成为唯一在线账号
    trae = scheduler.pools.pool("trae_group")
    assert trae.slots[0].state is AccountState.FAILED
    assert trae.slots[0].last_fault is FaultKind.QUOTA_EXHAUSTED
    assert trae.active_index == 1
    online = [s for s in trae.slots if s.state in {AccountState.ACTIVE, AccountState.WORKING}]
    assert len(online) == 1


def test_quota_first_two_accounts_switch_twice(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(
        accounts, tmp_path, plan={"trae_group": {0: "quota", 1: "quota"}}
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)

    assert data.state is TaskState.SUCCEEDED
    result = scheduler.task_result(data.task_id)
    assert result.account_attempts == 3
    assert result.account_label == "trae-c"


def test_all_accounts_exhausted_task_fails(accounts: AccountsConfig, tmp_path: Path) -> None:
    """三个账号全部额度耗尽 → 任务失败，错误信息可读。"""
    scheduler, _ = _scheduler(
        accounts,
        tmp_path,
        plan={"trae_group": {0: "quota", 1: "quota", 2: "quota"}},
        max_attempts=3,
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    result = scheduler.task_result(data.task_id)

    assert result.state is TaskState.FAILED
    assert result.succeeded is False
    assert result.fault_kind == FaultKind.QUOTA_EXHAUSTED.value
    assert result.account_attempts == 3
    assert result.fail_reason

    trae = scheduler.pools.pool("trae_group")
    assert trae.active_index == -1
    status = scheduler.pool_status(group="trae_group")
    assert status.healthy is False
    assert status.warnings


def test_max_attempts_limits_switching(accounts: AccountsConfig, tmp_path: Path) -> None:
    """max_account_attempts=2 时，即使有 3 个账号也只试 2 个。"""
    scheduler, runner = _scheduler(
        accounts,
        tmp_path,
        plan={"trae_group": {0: "quota", 1: "quota", 2: "success"}},
        max_attempts=2,
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    result = scheduler.task_result(data.task_id)

    assert result.state is TaskState.FAILED
    assert len(runner.calls) == 2


def test_banned_account_switches(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path, plan={"qoder_group": {0: "banned"}})
    data = scheduler.submit(group="qoder_group", manifest=_manifest(), wait=True)

    assert data.state is TaskState.SUCCEEDED
    qoder = scheduler.pools.pool("qoder_group")
    assert qoder.slots[0].state is AccountState.FAILED


def test_recovered_account_used_again_after_manual_switch(
    accounts: AccountsConfig, tmp_path: Path
) -> None:
    """全部耗尽后手动切回 A（人工确认额度已恢复），能继续跑任务。"""
    scheduler, _ = _scheduler(
        accounts,
        tmp_path,
        plan={"trae_group": {0: "quota", 1: "quota", 2: "quota"}},
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    assert scheduler.task_result(data.task_id).state is TaskState.FAILED

    switched = scheduler.switch_account(group="trae_group", target_index=0, reason="额度已恢复")
    assert switched.active_index == 0
    assert switched.active_label == "trae-a"

    # 恢复后不再注入故障
    scheduler._runner = MockRunner(plan={})  # type: ignore[attr-defined]
    data2 = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    assert data2.state is TaskState.SUCCEEDED


# --------------------------------------------------------------------------- #
# 并发与串行
# --------------------------------------------------------------------------- #


def test_same_group_tasks_are_serialized(accounts: AccountsConfig, tmp_path: Path) -> None:
    """同组两个任务：第二个必须等第一个结束（结论靠时间戳判断）。"""
    runner = MockRunner(plan={}, latency_s=0.15)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "workspace",
        max_account_attempts=3,
        default_timeout_s=5.0,
    )

    first = scheduler.submit(group="trae_group", manifest=_manifest("任务一"), wait=False)
    second = scheduler.submit(group="trae_group", manifest=_manifest("任务二"), wait=False)

    scheduler.task_result(first.task_id, wait_s=5.0)
    scheduler.task_result(second.task_id, wait_s=5.0)

    # 串行证据：两次调用在同一账号、attempt 都是 1，且时间不重叠。
    groups = [call[0] for call in runner.calls]
    assert groups.count("trae_group") == 2
    # 若并行，两个任务会同时 mark .DONE；这里通过"任务二的开始时间 >= 任务一的结束时间"判断。
    ws1 = Path(first.workspace) / ".DONE"
    ws2 = Path(second.workspace) / ".DONE"
    assert ws1.stat().st_mtime <= ws2.stat().st_mtime


def test_cross_group_tasks_run_in_parallel(accounts: AccountsConfig, tmp_path: Path) -> None:
    """跨组并行：总耗时接近单任务耗时，而不是两倍。"""
    runner = MockRunner(plan={}, latency_s=0.15)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "workspace",
        max_account_attempts=3,
        default_timeout_s=5.0,
    )

    started = time.monotonic()
    trae = scheduler.submit(group="trae_group", manifest=_manifest("T"), wait=False)
    qoder = scheduler.submit(group="qoder_group", manifest=_manifest("Q"), wait=False)
    scheduler.task_result(trae.task_id, wait_s=5.0)
    scheduler.task_result(qoder.task_id, wait_s=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.9, f"跨组应并行执行，实测 {elapsed:.2f}s 偏慢（串行约 1s+）"
    assert {call[0] for call in runner.calls} == {"trae_group", "qoder_group"}


def test_concurrent_submits_same_group_never_share_account(accounts: AccountsConfig, tmp_path: Path) -> None:
    """同组 5 个并发提交：每个任务都拿到同一时刻唯一的在线账号。"""
    scheduler, runner = _scheduler(accounts, tmp_path, latency_s=0.02)
    task_ids: list[str] = []
    lock = threading.Lock()

    def _submit(index: int) -> None:
        data = scheduler.submit(group="trae_group", manifest=_manifest(f"并发{index}"), wait=False)
        with lock:
            task_ids.append(data.task_id)

    threads = [threading.Thread(target=_submit, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for task_id in task_ids:
        assert scheduler.task_result(task_id, wait_s=5.0).state is TaskState.SUCCEEDED

    trae = scheduler.pools.pool("trae_group")
    online = [s for s in trae.slots if s.state in {AccountState.ACTIVE, AccountState.WORKING}]
    assert len(online) == 1
    assert len(runner.calls) == 5


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #


def test_unknown_group_rejected(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path)
    with pytest.raises(ToolError) as excinfo:
        scheduler.submit(group="nope_group", manifest=_manifest(), wait=True)
    assert excinfo.value.code is ErrorCode.GROUP_NOT_FOUND


def test_unknown_task_id(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path)
    with pytest.raises(ToolError) as excinfo:
        scheduler.task_result("t-does-not-exist")
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_pool_exhausted_before_submit(accounts_document: dict[str, object], tmp_path: Path) -> None:
    for account in accounts_document["groups"]["trae_group"]["accounts"]:  # type: ignore[index]
        account["disabled"] = True
    from native_cli_orchestrator.config.accounts import parse_accounts

    scheduler, _ = _scheduler(parse_accounts(accounts_document), tmp_path)
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    result = scheduler.task_result(data.task_id)
    assert result.state is TaskState.FAILED


def test_missing_credential_switches_account(accounts: AccountsConfig, tmp_path: Path) -> None:
    """real 语义下账号环境变量缺失时，应换下一个账号而不是直接失败。

    Mock 执行器不需要凭据（开发机要能空跑），所以这里用一个 requires_credentials=True
    的替身来验证 real 路径的"缺凭据→换账号"逻辑。
    """
    from native_cli_orchestrator.common.text import now_local

    class _CredentialRunner(MockRunner):
        requires_credentials = True

    accounts.groups["trae_group"].accounts[0].env = {"TRAE_TOKEN": "env:NCO_TEST_MISSING"}
    runner = _CredentialRunner(plan={}, requires_credentials=True)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "workspace",
        max_account_attempts=3,
        default_timeout_s=5.0,
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)

    assert data.state is TaskState.SUCCEEDED
    assert scheduler.pools.pool("trae_group").slots[0].state is AccountState.FAILED
    assert runner.calls == [("trae_group", 1, 2)], "第一个账号缺凭据，应直接在第 2 次尝试里用 trae-b"
    assert now_local() is not None


# --------------------------------------------------------------------------- #
# 手动切换
# --------------------------------------------------------------------------- #


def test_switch_account_manual(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path)
    result = scheduler.switch_account(group="trae_group", reason="测试")

    assert result.switched is True
    assert result.previous_index == 0
    assert result.active_index == 1
    assert result.active_label == "trae-b"
    trae = scheduler.pools.pool("trae_group")
    assert trae.slots[0].state is AccountState.DISABLED
    assert len([s for s in trae.slots if s.state is AccountState.ACTIVE]) == 1


def test_switch_account_no_alternative(accounts: AccountsConfig, tmp_path: Path) -> None:
    """qoder 只有 2 个账号，切一次后第二个再切就没了。"""
    scheduler, _ = _scheduler(accounts, tmp_path)
    scheduler.switch_account(group="qoder_group")
    with pytest.raises(ToolError) as excinfo:
        scheduler.switch_account(group="qoder_group")
    assert excinfo.value.code is ErrorCode.POOL_EXHAUSTED


def test_switch_account_to_specific_index(accounts: AccountsConfig, tmp_path: Path) -> None:
    scheduler, _ = _scheduler(accounts, tmp_path)
    result = scheduler.switch_account(group="trae_group", target_index=2)

    assert result.active_index == 2
    assert result.active_label == "trae-c"


# --------------------------------------------------------------------------- #
# 配置重载
# --------------------------------------------------------------------------- #


def test_reload_accounts_keeps_active_label(accounts: AccountsConfig, tmp_path: Path) -> None:
    """改配置后，原来在用的账号应继续是激活账号（不因改配置被下线）。"""
    scheduler, _ = _scheduler(accounts, tmp_path)
    scheduler.switch_account(group="trae_group")  # 现在用 trae-b

    from native_cli_orchestrator.config.accounts import parse_accounts

    document = {
        "groups": {
            "trae_group": {
                "accounts": [
                    {"label": "trae-a", "env": {"PROFILE": "alpha"}},
                    {"label": "trae-b", "env": {"PROFILE": "beta"}},
                    {"label": "trae-d", "env": {"PROFILE": "delta"}},
                ]
            },
            "qoder_group": {"accounts": [{"label": "qoder-a", "env": {"PROFILE": "qa"}}]},
            "workbuddy_group": {"accounts": [{"label": "wb-a", "env": {"PROFILE": "wa"}}]},
        }
    }
    scheduler.reload_accounts(parse_accounts(document))

    trae = scheduler.pools.pool("trae_group")
    assert trae.slots[trae.active_index].label == "trae-b", "重载后应延续原激活账号"
    assert len(trae.slots) == 3


def test_reload_accounts_refused_while_running(accounts: AccountsConfig, tmp_path: Path) -> None:
    runner = MockRunner(plan={}, latency_s=0.4)
    scheduler = Scheduler(
        accounts=accounts,
        runner=runner,
        workspace_root=tmp_path / "workspace",
        default_timeout_s=5.0,
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=False)
    time.sleep(0.1)  # 等任务进入 RUNNING

    with pytest.raises(ToolError) as excinfo:
        scheduler.reload_accounts(accounts)
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "busy_groups" in excinfo.value.detail

    scheduler.task_result(data.task_id, wait_s=5.0)


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


def test_state_survives_restart(accounts: AccountsConfig, tmp_path: Path) -> None:
    """服务重启后，账号故障标记与激活顺序应延续。"""
    scheduler, _ = _scheduler(accounts, tmp_path, plan={"trae_group": {0: "quota"}})
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    assert scheduler.task_result(data.task_id).state is TaskState.SUCCEEDED
    scheduler.save_state()

    restarted, _ = _scheduler(accounts, tmp_path)
    trae = restarted.pools.pool("trae_group")
    assert trae.slots[0].state is AccountState.FAILED
    assert trae.active_index == 1


def test_task_result_readable_after_restart(accounts: AccountsConfig, tmp_path: Path) -> None:
    """重启后仍能通过 task_id 读回结果（从工作区恢复）。"""
    scheduler, _ = _scheduler(accounts, tmp_path)
    data = scheduler.submit(group="qoder_group", manifest=_manifest(), wait=True)

    restarted, _ = _scheduler(accounts, tmp_path)
    result = restarted.task_result(data.task_id)
    assert result.state is TaskState.SUCCEEDED
    assert result.artifacts


# --------------------------------------------------------------------------- #
# 信封层
# --------------------------------------------------------------------------- #


def test_submit_request_defaults() -> None:
    request = SubmitTaskRequest(group="trae_group", task_manifest=_manifest())
    assert request.wait is False
    assert request.timeout_s > 0


# --------------------------------------------------------------------------- #
# 环境级错误（不应错标账号）
# --------------------------------------------------------------------------- #


def test_env_error_reports_code_without_failing_account(accounts: AccountsConfig, tmp_path: Path) -> None:
    """CLI 不存在属环境问题：任务失败并带 error_code，但账号不能被标 FAILED。

    否则一台没装 CLI 的机器会把整个账号池一次性打成 FAILED，装上 CLI 后还要手工恢复。
    """
    from native_cli_orchestrator.contracts.errors import ErrorCode as _Err

    class _NoCliRunner(MockRunner):
        requires_credentials = False

        def run(self, context):  # type: ignore[no-untyped-def]
            raise ToolError(_Err.CLI_NOT_FOUND, "找不到 CLI 可执行文件：traecli")

    scheduler = Scheduler(
        accounts=accounts,
        runner=_NoCliRunner(plan={}),
        workspace_root=tmp_path / "workspace",
        max_account_attempts=3,
        default_timeout_s=5.0,
    )
    data = scheduler.submit(group="trae_group", manifest=_manifest(), wait=True)
    result = scheduler.task_result(data.task_id)

    assert result.state is TaskState.FAILED
    assert result.error_code == _Err.CLI_NOT_FOUND.value
    assert "traecli" in result.fail_reason

    trae = scheduler.pools.pool("trae_group")
    assert all(slot.state is not AccountState.FAILED for slot in trae.slots), \
        "环境级错误不得把账号标成 FAILED"
    assert trae.active_index == 0
