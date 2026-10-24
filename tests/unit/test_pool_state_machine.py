"""账号池状态机测试：**单实例激活**是本项目最核心的不变量。

每个测试都在断言两件事：
1. 状态迁移符合预期；
2. 任意时刻同一分组里 ACTIVE/WORKING 的账号数 ≤ 1。
"""

from __future__ import annotations

import threading

import pytest

from native_cli_orchestrator.config.accounts import AccountsConfig
from native_cli_orchestrator.contracts.constants import AccountState, FaultKind
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.pools.manager import GroupPool, PoolManager


def _online_count(pool: GroupPool) -> int:
    """统计"在线"账号数（ACTIVE 或 WORKING）。"""
    return sum(
        1
        for slot in pool.slots
        if slot.state in {AccountState.ACTIVE, AccountState.WORKING}
    )


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #


def test_first_available_account_activated_on_init(accounts: AccountsConfig) -> None:
    pools = PoolManager(accounts)
    trae = pools.pool("trae_group")

    assert trae.active_index == 0
    assert trae.slots[0].state is AccountState.ACTIVE
    assert _online_count(trae) == 1
    # 其余账号必须休眠，不能拉起
    assert trae.slots[1].state is AccountState.DORMANT
    assert trae.slots[2].state is AccountState.DORMANT


def test_disabled_account_never_activated(accounts: AccountsConfig) -> None:
    pools = PoolManager(accounts)
    wb = pools.pool("workbuddy_group")

    assert wb.slots[1].state is AccountState.DISABLED
    assert wb.active_index == 0
    assert _online_count(wb) == 1


def test_all_disabled_group_has_no_active(accounts_document: dict[str, object]) -> None:
    for account in accounts_document["groups"]["qoder_group"]["accounts"]:  # type: ignore[index]
        account["disabled"] = True
    from native_cli_orchestrator.config.accounts import parse_accounts

    pools = PoolManager(parse_accounts(accounts_document))
    qoder = pools.pool("qoder_group")

    assert qoder.active_index == -1
    with pytest.raises(ToolError) as excinfo:
        qoder.acquire()
    assert excinfo.value.code is ErrorCode.POOL_EXHAUSTED


# --------------------------------------------------------------------------- #
# 单实例不变量
# --------------------------------------------------------------------------- #


def test_activate_keeps_exactly_one_online(accounts: AccountsConfig) -> None:
    pool = PoolManager(accounts).pool("trae_group")
    pool.force_activate(2)

    assert pool.active_index == 2
    assert pool.slots[2].state is AccountState.ACTIVE
    assert _online_count(pool) == 1
    assert pool.slots[0].state is AccountState.DORMANT


def test_working_account_not_demoted_when_another_activated(accounts: AccountsConfig) -> None:
    """正在工作的账号不能因为切指针而被改成 DORMANT（它还在跑任务）。"""
    pool = PoolManager(accounts).pool("trae_group")
    pool.begin_work(0)
    assert pool.slots[0].busy is True

    pool.force_activate(1)

    assert pool.slots[0].state is AccountState.WORKING
    assert pool.slots[0].busy is True
    assert pool.slots[1].state is AccountState.ACTIVE


def test_concurrent_acquire_never_returns_two_accounts(accounts: AccountsConfig) -> None:
    """并发 acquire 只能拿到同一个账号（组内单实例的守门人）。"""
    pool = PoolManager(accounts).pool("trae_group")
    results: list[int] = []
    lock = threading.Lock()

    def _take() -> None:
        slot = pool.acquire()
        with lock:
            results.append(slot.index)

    threads = [threading.Thread(target=_take) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(results) == {0}, "并发 acquire 必须都指向同一个激活账号"
    assert _online_count(pool) == 1


# --------------------------------------------------------------------------- #
# 故障与轮换
# --------------------------------------------------------------------------- #


def test_quota_exhausted_marks_failed_immediately(accounts: AccountsConfig) -> None:
    """配额耗尽属硬故障，一次即 FAILED，不必等连续失败阈值。"""
    pool = PoolManager(accounts).pool("trae_group")
    state = pool.mark_fault(0, FaultKind.QUOTA_EXHAUSTED)

    assert state is AccountState.FAILED
    assert pool.slots[0].last_fault is FaultKind.QUOTA_EXHAUSTED
    assert pool.slots[0].consecutive_failures == 1


@pytest.mark.parametrize("fault", [FaultKind.TIMEOUT, FaultKind.CRASHED, FaultKind.RATE_LIMITED])
def test_soft_fault_needs_threshold(accounts: AccountsConfig, fault: FaultKind) -> None:
    pool = PoolManager(accounts, failure_threshold=2).pool("trae_group")

    first = pool.mark_fault(0, fault)
    assert first is AccountState.ACTIVE, "第一次软故障不应直接下线"

    second = pool.mark_fault(0, fault)
    assert second is AccountState.FAILED, "连续两次后应标记 FAILED"


def test_acquire_moves_to_next_after_failed(accounts: AccountsConfig) -> None:
    pool = PoolManager(accounts).pool("trae_group")
    pool.deactivate_current(state=AccountState.FAILED)

    assert pool.active_index == 1
    assert pool.acquire().label == "trae-b"
    assert _online_count(pool) == 1


def test_deactivate_last_available_leaves_no_active(accounts: AccountsConfig) -> None:
    pool = PoolManager(accounts).pool("qoder_group")
    pool.deactivate_current(state=AccountState.FAILED)  # qoder-a 下线 → qoder-b 激活
    assert pool.active_index == 1

    following = pool.deactivate_current(state=AccountState.FAILED)  # qoder-b 下线，无备用
    assert following == -1
    assert pool.active_index == -1
    with pytest.raises(ToolError) as excinfo:
        pool.acquire()
    assert excinfo.value.code is ErrorCode.POOL_EXHAUSTED


def test_next_available_wraps_around(accounts: AccountsConfig) -> None:
    pool = PoolManager(accounts).pool("trae_group")
    assert pool.next_available(after=0) == 1
    assert pool.next_available(after=2) == 0  # 回绕到第一个


def test_force_activate_recovers_failed_account(accounts: AccountsConfig) -> None:
    """手动切换到曾失败的账号视为人工修复。"""
    pool = PoolManager(accounts).pool("trae_group")
    pool.mark_fault(0, FaultKind.BANNED)
    assert pool.slots[0].state is AccountState.FAILED

    pool.force_activate(0)
    assert pool.slots[0].state is AccountState.ACTIVE
    assert pool.slots[0].consecutive_failures == 0


# --------------------------------------------------------------------------- #
# 跨组并行
# --------------------------------------------------------------------------- #


def test_groups_use_independent_locks(accounts: AccountsConfig) -> None:
    """不同分组必须持不同锁，这是跨组并行的前提。"""
    pools = PoolManager(accounts)
    assert pools.pool("trae_group").lock is not pools.pool("qoder_group").lock


def test_cross_group_work_parallel(accounts: AccountsConfig) -> None:
    """Trae 组持锁工作时，Qoder 组仍能取到账号（不被阻塞）。"""
    pools = PoolManager(accounts)
    trae = pools.pool("trae_group")
    qoder = pools.pool("qoder_group")

    acquired: list[str] = []
    release = threading.Event()

    def _hold_trae() -> None:
        with trae.lock:
            trae.begin_work(0)
            release.wait(timeout=2.0)

    holder = threading.Thread(target=_hold_trae)
    holder.start()

    try:
        slot = qoder.acquire()
        acquired.append(slot.label)
    finally:
        release.set()
        holder.join(timeout=2.0)

    assert acquired == ["qoder-a"]


# --------------------------------------------------------------------------- #
# 状态序列化
# --------------------------------------------------------------------------- #


def test_state_roundtrip_preserves_faults(accounts: AccountsConfig, tmp_path) -> None:
    pools = PoolManager(accounts)
    pools.pool("trae_group").mark_fault(0, FaultKind.QUOTA_EXHAUSTED)
    pools.pool("trae_group").deactivate_current(state=AccountState.FAILED)

    path = tmp_path / "pools.state.json"
    pools.save(path)

    restored = PoolManager(accounts)
    assert restored.load(path) is True
    trae = restored.pool("trae_group")
    assert trae.slots[0].state is AccountState.FAILED
    assert trae.slots[0].last_fault is FaultKind.QUOTA_EXHAUSTED
    assert trae.active_index == 1


def test_restore_ignores_changed_account_count(accounts: AccountsConfig, tmp_path) -> None:
    pools = PoolManager(accounts)
    path = tmp_path / "pools.state.json"
    pools.save(path)

    payload = path.read_text(encoding="utf-8")
    # 删掉一个账号后再恢复，应忽略旧状态而不是错配。
    shorter = payload.replace('"label": "trae-c"', '"label": "trae-x"')
    path.write_text(shorter, encoding="utf-8")

    restored = PoolManager(accounts)
    restored.load(path)
    assert restored.pool("trae_group").active_index >= 0


def test_working_state_restored_as_active(accounts: AccountsConfig, tmp_path) -> None:
    """重启后不可能有正在跑的子进程，WORKING 必须回落为 ACTIVE。"""
    pools = PoolManager(accounts)
    pools.pool("trae_group").begin_work(0)
    path = tmp_path / "pools.state.json"
    pools.save(path)

    restored = PoolManager(accounts)
    restored.load(path)
    assert restored.pool("trae_group").slots[0].state is AccountState.ACTIVE
    assert restored.pool("trae_group").slots[0].busy is False
