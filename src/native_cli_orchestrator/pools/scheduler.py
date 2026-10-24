"""调度器：把「分组 → 账号 → 执行器」串起来。

三条不变量：
1. **组内串行**：同组同一时刻只有一个任务在跑，持组锁；
2. **组内单实例**：只有 ``active_index`` 对应的账号在线，其余 DORMANT；
3. **跨组并行**：不同组各持自己的锁，互不阻塞。

失败处理：可切换故障（配额/封禁/鉴权/限流/超时/崩溃）→ 标记账号 FAILED → 换下一个备用账号重试，
最多尝试 ``max_account_attempts`` 个账号；不可切换故障（环境类）→ 任务直接失败。

任务在后台线程执行，``submit_task(wait=False)`` 立刻返回 task_id，由 ``get_task_result`` 轮询——
这样 DSH 不必为一个长任务挂住连接。
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..common.ids import new_task_id
from ..common.text import now_local
from ..config.accounts import AccountsConfig
from ..contracts.constants import (
    AccountState,
    FaultKind,
    TaskState,
)
from ..contracts.errors import ErrorCode, ToolError
from ..contracts.tools import (
    GetPoolStatusData,
    GetTaskResultData,
    GroupStatus,
    SubmitTaskData,
    SwitchAccountData,
    TaskArtifact,
    TaskManifest,
)
from ..observability.logging import get_logger
from ..pools.manager import PoolManager
from ..runners.base import RunContext, Runner
from ..tasks.workspace import TaskRecord, TaskWorkspace

_log = get_logger("dao.scheduler")


class Scheduler:
    """账号池调度器（单一入口，供工具层调用）。"""

    def __init__(
        self,
        *,
        accounts: AccountsConfig,
        runner: Runner,
        workspace_root: Path,
        state_path: Path | None = None,
        max_account_attempts: int = 3,
        default_timeout_s: float = 900.0,
        group_cooldown_s: float = 0.0,
        switch_cooldown_s: float = 0.0,
        failure_threshold: int = 2,
    ) -> None:
        self._accounts = accounts
        self._runner = runner
        self._workspace_root = workspace_root
        self._state_path = state_path
        self._max_attempts = max(1, max_account_attempts)
        self._default_timeout_s = default_timeout_s
        self._group_cooldown_s = group_cooldown_s
        self._switch_cooldown_s = switch_cooldown_s

        self._pools = PoolManager(accounts, failure_threshold=failure_threshold)
        if state_path is not None and state_path.is_file():
            self._pools.load(state_path)
        # 让复合执行器知道每个分组是 cli 还是 api。
        bind = getattr(self._runner, "bind_groups", None)
        if callable(bind):
            bind(self._pools.kinds)

        #: 任务表（内存 + 落盘）。
        self._tasks: dict[str, TaskRecord] = {}
        self._tasks_lock = threading.Lock()
        #: 每组最后完成时间，用于风控冷却。
        self._last_finished: dict[str, float] = defaultdict(float)
        #: 运行中的任务线程。
        self._threads: dict[str, threading.Thread] = {}
        #: 组级队列计数（用于 get_pool_status）。
        self._queue: dict[str, int] = defaultdict(int)

        workspace_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 查询

    @property
    def pools(self) -> PoolManager:
        return self._pools

    @property
    def accounts(self) -> AccountsConfig:
        return self._accounts

    def pool_status(self, *, group: str | None = None) -> GetPoolStatusData:
        statuses = self._pools.statuses(only=group)
        self._attach_api_budget(statuses)
        warnings: list[str] = []
        for status in statuses:
            if status.active_index < 0:
                warnings.append(f"{status.group} 无可用账号，需人工处理")
            elif status.queued:
                warnings.append(f"{status.group} 有 {status.queued} 个任务排队")
            budget = status.api_budget
            if budget and budget.get("daily_budget"):
                remaining = budget.get("budget_remaining")
                if remaining is not None and remaining <= 0:
                    warnings.append(
                        f"{status.group} 今日 API 预算已用尽"
                        f"（已花 {budget.get('cost_today', 0):.4f} 元）"
                    )
        return GetPoolStatusData(
            groups=statuses,
            healthy=not any(status.active_index < 0 for status in statuses),
            warnings=warnings,
        )

    def _attach_api_budget(self, statuses: list[GroupStatus]) -> None:
        """给 api 分组附上当日用量（执行器持有账本，调度器只做展示）。"""
        summary_fn = getattr(self._runner, "budget_summary", None)
        if not callable(summary_fn):
            return
        for status in statuses:
            try:
                group_config = self._accounts.group(status.group)
            except ToolError:
                continue
            if not group_config.is_api or group_config.api is None:
                continue
            try:
                status.api_budget = summary_fn(status.group, group_config.api.daily_budget)
            except Exception:  # noqa: BLE001 - 展示用，不能因为账本问题让状态查询失败
                status.api_budget = {}

    def task_result(self, task_id: str, *, wait_s: float = 0.0) -> GetTaskResultData:
        """取任务结果；``wait_s>0`` 时最多等待该时长让任务收尾。"""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            record = self._get_record(task_id)
            if record.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
                return self._to_result(record)
            if time.monotonic() >= deadline:
                return self._to_result(record)
            time.sleep(0.05)

    def _get_record(self, task_id: str) -> TaskRecord:
        with self._tasks_lock:
            record = self._tasks.get(task_id)
        if record is None:
            # 内存里没有则尝试从工作区恢复（服务重启后仍能取结果）。
            record = self._load_record_from_disk(task_id)
        if record is None:
            raise ToolError(
                ErrorCode.TASK_NOT_FOUND,
                f"任务不存在：{task_id}",
                detail={"task_id": task_id},
            )
        return record

    def _load_record_from_disk(self, task_id: str) -> TaskRecord | None:
        root = self._workspace_root / task_id
        state_file = root / ".task.json"
        if not state_file.is_file():
            return None
        import json

        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        record = TaskRecord.from_json(payload, workspace_root=self._workspace_root)
        if record is not None:
            with self._tasks_lock:
                self._tasks[task_id] = record
        return record

    def _to_result(self, record: TaskRecord) -> GetTaskResultData:
        artifacts: list[TaskArtifact] = []
        if record.state is TaskState.SUCCEEDED:
            try:
                manifest = record.workspace.read_manifest()
                artifacts = record.workspace.collect_artifacts(expected=manifest.artifacts)
            except ToolError:  # 清单丢失不影响取结果
                artifacts = record.workspace.collect_artifacts()
        return GetTaskResultData(
            task_id=record.task_id,
            group=record.group,
            state=record.state,
            succeeded=record.state is TaskState.SUCCEEDED,
            account_index=record.account_index,
            account_label=record.account_label,
            summary=record.summary,
            artifacts=artifacts,
            fail_reason=record.fail_reason,
            fault_kind=record.fault_kind,
            error_code=record.error_code,
            exit_code=record.exit_code,
            elapsed_s=record.elapsed_s,
            account_attempts=record.account_attempts,
            updated_at=_parse_dt(record.updated_at),
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            cost_yuan=record.cost_yuan,
            truncated=record.truncated,
        )

    # ------------------------------------------------------------------ 提交

    def submit(
        self,
        *,
        group: str,
        manifest: TaskManifest,
        timeout_s: float | None = None,
        wait: bool = False,
    ) -> SubmitTaskData:
        """提交任务。

        ``wait=False`` 时立刻返回（任务在后台线程跑）；``wait=True`` 时阻塞到结束。
        """
        pool = self._pools.pool(group)
        timeout = timeout_s or self._default_timeout_s

        # 先在组锁内占位，保证"组内串行"从提交那一刻就生效。
        task_id = new_task_id(group)
        workspace = TaskWorkspace.create(self._workspace_root, task_id, group)
        workspace.write_manifest(manifest)
        workspace.write_prompt(manifest)

        # 预定账号：此处不实际执行，只记录将使用哪个账号（真正执行时再 acquire）。
        reserved = pool.active_index
        record = TaskRecord(
            task_id=task_id,
            group=group,
            state=TaskState.QUEUED,
            account_index=reserved,
            account_label=pool.slots[reserved].label if reserved >= 0 else "",
            workspace=workspace,
            submitted_at=now_local().isoformat(),
            updated_at=now_local().isoformat(),
        )
        self._persist(record)
        self._queue[group] += 1
        pool.set_queue(queued=self._queue[group])

        _log.info(
            "提交任务 task=%s group=%s 预定账号=%s timeout=%.0fs wait=%s",
            task_id,
            group,
            record.account_label or "-",
            timeout,
            wait,
        )

        if wait:
            self._execute(record, manifest=manifest, timeout_s=timeout)
            return self._to_submit_data(record, waited=True)

        thread = threading.Thread(
            target=self._execute,
            kwargs={"record": record, "manifest": manifest, "timeout_s": timeout},
            name=f"task-{task_id}",
            daemon=True,
        )
        self._threads[task_id] = thread
        thread.start()
        return self._to_submit_data(record, waited=False)

    def _to_submit_data(self, record: TaskRecord, *, waited: bool) -> SubmitTaskData:
        return SubmitTaskData(
            task_id=record.task_id,
            group=record.group,
            state=record.state,
            account_index=record.account_index,
            account_label=record.account_label,
            workspace=str(record.workspace.root),
            submitted_at=_parse_dt(record.submitted_at) or now_local(),
            waited=waited,
        )

    # ------------------------------------------------------------------ 执行

    def _execute(self, record: TaskRecord, *, manifest: TaskManifest, timeout_s: float) -> None:
        """真正执行：组锁内串行，失败按策略换账号。"""
        group = record.group
        pool = self._pools.pool(group)
        started = time.monotonic()

        try:
            # 组锁：同组任务串行（跨组互不影响）。
            with pool.lock:
                self._queue[group] = max(0, self._queue[group] - 1)
                pool.set_queue(queued=self._queue[group], running=1)
                self._update(record, state=TaskState.RUNNING)

                self._respect_cooldown(group)

                attempts = 0
                last_fault: FaultKind | None = None
                last_reason = ""

                group_config = self._accounts.group(group)
                is_api_group = group_config.is_api
                api_spec = group_config.api

                while attempts < self._max_attempts:
                    attempts += 1
                    slot = pool.acquire()
                    record.account_index = slot.index
                    record.account_label = slot.label
                    record.account_attempts = attempts

                    try:
                        env = slot.config.resolve_env(dict(os.environ))
                        api_key = slot.config.api_key(dict(os.environ)) if is_api_group else ""
                    except ToolError as exc:
                        if not self._runner.requires_credentials:
                            # Mock 模式不需要凭据，缺环境变量不应影响调试。
                            env, api_key = {}, ""
                            _log.debug(
                                "mock 模式跳过凭据校验（account=%s）", slot.label
                            )
                        else:
                            # 账号缺凭据：标记故障并换下一个。
                            pool.mark_fault(slot.index, FaultKind.AUTH_INVALID, note=exc.message)
                            last_fault, last_reason = FaultKind.AUTH_INVALID, exc.message
                            continue

                    pool.begin_work(slot.index)
                    _log.info(
                        "派发 task=%s group=%s account=%s attempt=%d",
                        record.task_id,
                        group,
                        slot.label,
                        attempts,
                    )

                    context = RunContext(
                        task_id=record.task_id,
                        group=group,
                        manifest=manifest,
                        workspace=record.workspace,
                        account_index=slot.index,
                        account_label=slot.label,
                        env=env,
                        attempt=attempts,
                        timeout_s=timeout_s,
                        api=api_spec,
                        api_key=api_key,
                    )

                    try:
                        result = self._runner.run(context)
                    except ToolError:
                        pool.end_work(slot.index, succeeded=False)
                        raise

                    if result.succeeded:
                        pool.end_work(slot.index, succeeded=True)
                        record.state = TaskState.SUCCEEDED
                        record.summary = result.summary
                        record.exit_code = result.exit_code
                        record.elapsed_s = time.monotonic() - started
                        record.prompt_tokens = result.prompt_tokens
                        record.completion_tokens = result.completion_tokens
                        record.cost_yuan = result.cost_yuan
                        record.truncated = result.truncated
                        self._finish_success(record)
                        return

                    pool.end_work(slot.index, succeeded=False)
                    last_fault = result.fault
                    last_reason = result.reason
                    record.exit_code = result.exit_code

                    if result.fault is not None:
                        state = pool.mark_fault(slot.index, result.fault, note=result.reason[:200])
                        _log.warning(
                            "账号故障 task=%s group=%s account=%s fault=%s → 状态=%s",
                            record.task_id,
                            group,
                            slot.label,
                            result.fault.value,
                            state.value,
                        )

                    if not result.retry_with_other_account:
                        break

                    # 配额/封禁类：主动下线当前账号，切到下一个备用。
                    following = pool.deactivate_current(
                        state=AccountState.FAILED,
                        note=f"{result.fault.value if result.fault else 'failed'}",
                    )
                    if following < 0:
                        _log.warning("group=%s 已无备用账号可切换", group)
                        break

                    self._switch_cooldown(group)

                # 所有尝试都失败
                record.state = TaskState.FAILED
                record.fail_reason = last_reason or "全部账号尝试失败"
                record.fault_kind = last_fault.value if last_fault else ""
                record.elapsed_s = time.monotonic() - started
                record.workspace.mark_failed(reason=record.fail_reason)
                self._persist(record)
                pool.set_queue(running=0)
                self._last_finished[group] = time.monotonic()
                _log.warning(
                    "任务失败 task=%s group=%s 尝试=%d fault=%s",
                    record.task_id,
                    group,
                    attempts,
                    record.fault_kind or "-",
                )

        except ToolError as exc:
            record.state = TaskState.FAILED
            record.fail_reason = exc.message
            record.fault_kind = ""
            # 环境级错误（CLI 不存在 / 依赖缺失 / 配置非法）不是"任务的锅"，
            # 用 error_code 与业务失败区分，避免编排侧误以为是模型没干活。
            record.error_code = exc.code.value
            record.elapsed_s = time.monotonic() - started
            self._persist(record)
            pool.set_queue(running=0)
            _log.error("任务异常终止 task=%s: %s", record.task_id, exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜底：调度线程不能静默死掉
            record.state = TaskState.FAILED
            record.fail_reason = f"内部错误：{type(exc).__name__}"
            record.elapsed_s = time.monotonic() - started
            self._persist(record)
            pool.set_queue(running=0)
            _log.exception("任务内部错误 task=%s", record.task_id)
        finally:
            with self._tasks_lock:
                self._threads.pop(record.task_id, None)

    def _finish_success(self, record: TaskRecord) -> None:
        # 汇总：把 CLI 写的 result.json（若有）与调度器状态合并，留给上层取。
        payload = {
            "task_id": record.task_id,
            "group": record.group,
            "succeeded": True,
            "account_index": record.account_index,
            "account_label": record.account_label,
            "summary": record.summary,
            "account_attempts": record.account_attempts,
            "elapsed_s": round(record.elapsed_s, 3),
            "finished_at": now_local().isoformat(),
        }
        record.workspace.write_result(payload)
        self._persist(record)
        pool = self._pools.pool(record.group)
        pool.set_queue(running=0)
        self._last_finished[record.group] = time.monotonic()
        _log.info(
            "任务成功 task=%s group=%s account=%s 尝试=%d 用时=%.1fs",
            record.task_id,
            record.group,
            record.account_label,
            record.account_attempts,
            record.elapsed_s,
        )

    def _respect_cooldown(self, group: str) -> None:
        """同组两次任务之间保持最小间隔（风控限流，需求阶段 5）。"""
        if self._group_cooldown_s <= 0:
            return
        last = self._last_finished.get(group, 0.0)
        if last <= 0:
            return
        remaining = self._group_cooldown_s - (time.monotonic() - last)
        if remaining > 0:
            _log.info("group=%s 冷却中，等待 %.1fs", group, remaining)
            time.sleep(remaining)

    def _switch_cooldown(self, group: str) -> None:
        if self._switch_cooldown_s > 0:
            _log.info("group=%s 切换账号后冷却 %.1fs", group, self._switch_cooldown_s)
            time.sleep(self._switch_cooldown_s)

    # ------------------------------------------------------------------ 手动切换

    def switch_account(
        self, *, group: str, target_index: int | None = None, reason: str = "manual"
    ) -> SwitchAccountData:
        """手动强制切换分组账号。

        - ``target_index`` 指定时直接切到该序号（即使它此前是 FAILED，人工介入视为已修复）；
        - 未指定时切到"下一个可用"；当前账号被标为 DISABLED（不参与自动调度）。
        """
        pool = self._pools.pool(group)
        with pool.lock:
            previous = pool.active_index
            previous_state: AccountState | None = None
            previous_label = pool.slots[previous].label if previous >= 0 else ""

            if target_index is None:
                following = pool.next_available(after=max(previous, 0))
                if following < 0:
                    raise ToolError(
                        ErrorCode.POOL_EXHAUSTED,
                        f"分组 {group} 没有其它可用账号可切换",
                        detail={"group": group, "active_index": previous},
                    )
                if previous >= 0:
                    previous_state = AccountState.DISABLED
                    pool.slot(previous).state = AccountState.DISABLED
                    pool.slot(previous).notes = f"手动切换下线（{reason}）"
                pool.force_activate(following)
                new_index = following
            else:
                # 越界检查由 pool.slot 负责。
                pool.slot(target_index)
                if previous >= 0 and previous != target_index:
                    previous_state = AccountState.DISABLED
                    pool.slot(previous).state = AccountState.DISABLED
                    pool.slot(previous).notes = f"手动切换下线（{reason}）"
                new_index = pool.force_activate(target_index)

            active = pool.slots[new_index]
            _log.info(
                "手动切换 group=%s %s → %s（reason=%s）",
                group,
                previous_label or "-",
                active.label,
                reason,
            )
            self._save_pool_state()
            return SwitchAccountData(
                group=group,
                previous_index=previous,
                active_index=new_index,
                active_label=active.label,
                previous_state=previous_state,
                switched=new_index != previous,
                notes=f"已切换到 {active.label}",
            )

    # ------------------------------------------------------------------ 持久化

    def _persist(self, record: TaskRecord) -> None:
        record.updated_at = now_local().isoformat()
        with self._tasks_lock:
            self._tasks[record.task_id] = record
        record.workspace.write_state(record.to_json())

    def _update(self, record: TaskRecord, *, state: TaskState) -> None:
        record.state = state
        self._persist(record)

    def _save_pool_state(self) -> None:
        if self._state_path is not None:
            self._pools.save(self._state_path)

    def reload_accounts(self, accounts: AccountsConfig) -> None:
        """热重载账号池配置。

        策略：新建 PoolManager 并尽量延续旧状态（按 label 对齐），
        这样"只改了备用账号顺序"这类操作不会把正在工作的账号打回 DORMANT。
        """
        with self._tasks_lock:
            busy = {record.group for record in self._tasks.values() if record.state is TaskState.RUNNING}
        if busy:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"有任务正在执行（{sorted(busy)}），请等任务结束再重载配置",
                detail={"busy_groups": sorted(busy)},
            )

        old_snapshot = self._pools.dump()
        new_pools = PoolManager(accounts)
        _carry_over_state(old_snapshot, new_pools)
        self._accounts = accounts
        self._pools = new_pools
        # 执行器也要重新绑定分组类型（可能新增/删除了 api 组）。
        bind = getattr(self._runner, "bind_groups", None)
        if callable(bind):
            bind(self._pools.kinds)
        self._save_pool_state()
        _log.info(
            "账号池配置已重载：%d 组 / %d 账号",
            len(accounts.groups),
            accounts.account_count,
        )

    def save_state(self) -> None:
        """服务退出或配置重载前保存账号池状态。"""
        self._save_pool_state()

    def shutdown(self) -> None:
        """停止调度：终止执行器、保存状态。"""
        self._runner.shutdown()
        for thread in list(self._threads.values()):
            thread.join(timeout=2.0)
        self._save_pool_state()
        _log.info("调度器已停止")


def _carry_over_state(snapshot: dict[str, Any], pools: PoolManager) -> None:
    """按 (group, label) 对齐，把旧状态的故障标记与使用时间带到新池。

    不搬运 ``active_index`` 而是尽量保持原激活账号：若旧激活账号在新配置里仍存在，
    它继续做本组当前账号，避免"改配置就把正在用的账号下线"。
    """
    groups = snapshot.get("groups") or {}
    for pool in pools.groups():
        raw_group = groups.get(pool.name) or {}
        old_accounts = raw_group.get("accounts") or []
        old_by_label = {str(item.get("label", "")): item for item in old_accounts}
        old_active_index = int(raw_group.get("active_index", -1))
        old_active_label = ""
        if 0 <= old_active_index < len(old_accounts):
            old_active_label = str(old_accounts[old_active_index].get("label", ""))

        for slot in pool.slots:
            raw = old_by_label.get(slot.label)
            if raw is None or slot.state is AccountState.DISABLED:
                continue
            slot.restore({**raw, "index": slot.index})

        if old_active_label:
            for slot in pool.slots:
                if slot.label == old_active_label and slot.available:
                    pool.force_activate(slot.index)
                    _log.info("group=%s 延续原激活账号 label=%s", pool.name, slot.label)
                    break


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


__all__ = ["Scheduler"]
