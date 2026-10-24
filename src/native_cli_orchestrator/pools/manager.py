"""账号池：分组内的单实例激活状态机。

**核心不变量**：同一分组任意时刻最多一个账号处于 ``ACTIVE`` / ``WORKING``，
其余账号保持 ``DORMANT``（进程不拉起、不登录、不发请求）。
每个分组持自己的锁，因此**跨分组可并行**，组内严格串行。

状态迁移：

    DORMANT --选中--> ACTIVE --派任务--> WORKING --成功--> ACTIVE
       ^                                     │
       │                                     ├--可切换故障--> FAILED（下移）
       └──────── 组内轮换回到它 ◄────────────┤
                                             └--手动切换--> DISABLED

FAILED / DISABLED 不会被自动选中，需 ``switch_account`` 或改配置恢复。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.text import now_local
from ..config.accounts import AccountConfig, AccountsConfig
from ..contracts.constants import (
    UNAVAILABLE_STATES,
    AccountState,
    FaultKind,
    GroupKind,
)
from ..contracts.errors import ErrorCode, ToolError
from ..contracts.tools import AccountStatus, GroupStatus
from ..observability.logging import get_logger

_log = get_logger("dao.scheduler")


@dataclass
class AccountSlot:
    """单个账号的运行期状态。"""

    index: int
    label: str
    config: AccountConfig
    state: AccountState = AccountState.DORMANT
    busy: bool = False
    last_fault: FaultKind | None = None
    last_fault_at: str = ""
    last_used_at: str = ""
    consecutive_failures: int = 0
    notes: str = ""

    @property
    def available(self) -> bool:
        return self.state not in UNAVAILABLE_STATES

    def to_status(self, *, active_index: int) -> AccountStatus:
        return AccountStatus(
            index=self.index,
            label=self.label,
            state=self.state,
            active=self.index == active_index,
            busy=self.busy,
            last_fault=self.last_fault,
            last_fault_at=_parse_dt(self.last_fault_at),
            last_used_at=_parse_dt(self.last_used_at),
            consecutive_failures=self.consecutive_failures,
            notes=self.notes,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            # label 必须持久化：配置重载时靠它把状态对齐到同一个人。
            "label": self.label,
            "state": self.state.value,
            "last_fault": self.last_fault.value if self.last_fault else "",
            "last_fault_at": self.last_fault_at,
            "last_used_at": self.last_used_at,
            "consecutive_failures": self.consecutive_failures,
            "notes": self.notes,
        }

    def restore(self, payload: dict[str, Any]) -> None:
        """从持久化状态恢复；``busy`` 不恢复（进程已随上次退出终止）。"""
        raw_state = payload.get("state")
        try:
            state = AccountState(str(raw_state))
        except ValueError:
            state = AccountState.DORMANT
        # 重启后不可能有正在执行的进程，WORKING 一律回落为 ACTIVE。
        if state is AccountState.WORKING:
            state = AccountState.ACTIVE
        self.state = state
        raw_fault = payload.get("last_fault") or ""
        self.last_fault = FaultKind(raw_fault) if raw_fault else None
        self.last_fault_at = str(payload.get("last_fault_at", ""))
        self.last_used_at = str(payload.get("last_used_at", ""))
        self.consecutive_failures = int(payload.get("consecutive_failures", 0))
        self.notes = str(payload.get("notes", ""))


def _parse_dt(raw: str) -> Any:
    """把 ISO 字符串还原成 datetime；空或非法返回 None。"""
    if not raw:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class GroupPool:
    """一个分组的账号池。

    线程安全：所有改动都在 ``self._lock`` 内；锁粒度是**单组**，
    因此 Trae 组在跑任务时不影响 Qoder 组取账号。
    """

    def __init__(
        self,
        name: str,
        config_group_accounts: list[AccountConfig],
        *,
        failure_threshold: int = 2,
        kind: GroupKind = GroupKind.CLI,
    ) -> None:
        self.name = name
        self.kind = kind
        self._lock = threading.RLock()
        self._failure_threshold = max(1, failure_threshold)
        self.slots: list[AccountSlot] = [
            AccountSlot(index=index, label=account.label, config=account)
            for index, account in enumerate(config_group_accounts)
        ]
        # 配置里 disabled 的账号直接落到 DISABLED。
        for slot in self.slots:
            if slot.config.disabled:
                slot.state = AccountState.DISABLED
                slot.notes = slot.notes or "配置中标记为 disabled"

        self._active_index: int = -1
        self._queued: int = 0
        self._running: int = 0
        # 首次取账号时自动激活第一个可用账号。
        self._activate_first_available()

    # ------------------------------------------------------------------ 属性

    @property
    def active_index(self) -> int:
        with self._lock:
            return self._active_index

    @property
    def lock(self) -> threading.RLock:
        """供调度器在"取账号 + 派任务"之间保持组内互斥。"""
        return self._lock

    def slot(self, index: int) -> AccountSlot:
        with self._lock:
            if index < 0 or index >= len(self.slots):
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"账号序号越界：{index}（分组 {self.name} 共 {len(self.slots)} 个账号）",
                    detail={"group": self.name, "index": index, "total": len(self.slots)},
                )
            return self.slots[index]

    def active_slot(self) -> AccountSlot | None:
        with self._lock:
            if self._active_index < 0:
                return None
            slot = self.slots[self._active_index]
            return slot if slot.available else None

    # ------------------------------------------------------------------ 选择

    def _activate_first_available(self) -> int:
        """按顺序找第一个可用账号并激活；没有则返回 -1。"""
        with self._lock:
            for slot in self.slots:
                if slot.available:
                    self._activate_locked(slot.index)
                    return slot.index
            self._active_index = -1
            return -1

    def _activate_locked(self, index: int) -> None:
        """把 ``index`` 设为唯一激活账号，其余 ACTIVE/WORKING 回落到 DORMANT。"""
        for slot in self.slots:
            if slot.index == index:
                if slot.state in {AccountState.ACTIVE, AccountState.WORKING}:
                    pass
                else:
                    slot.state = AccountState.ACTIVE
            elif slot.state in {AccountState.ACTIVE, AccountState.WORKING} and not slot.busy:
                slot.state = AccountState.DORMANT
        self._active_index = index

    def acquire(self) -> AccountSlot:
        """取得当前可派任务的账号；必要时自动激活下一个可用账号。

        这是"同组单实例"的守门人：没有可用账号时抛 ``POOL_EXHAUSTED``。
        """
        with self._lock:
            current = self.active_slot()
            if current is not None:
                return current

            # 当前账号不可用（被标 FAILED/DISABLED 或从未激活），尝试下移。
            start = max(self._active_index, 0)
            for offset in range(len(self.slots)):
                index = (start + offset) % len(self.slots)
                slot = self.slots[index]
                if slot.available:
                    _log.info(
                        "group=%s 激活账号 index=%d label=%s（原 index=%d 不可用）",
                        self.name,
                        slot.index,
                        slot.label,
                        self._active_index,
                    )
                    self._activate_locked(index)
                    return slot

            raise ToolError(
                ErrorCode.POOL_EXHAUSTED,
                f"分组 {self.name} 没有可用账号（全部 fault/disabled）",
                detail={
                    "group": self.name,
                    "accounts": [
                        {"index": s.index, "label": s.label, "state": s.state.value}
                        for s in self.slots
                    ],
                },
            )

    def next_available(self, *, after: int) -> int:
        """返回 ``after`` 之后的下一个可用账号序号；没有则 -1。

        **不会返回 ``after`` 本身**：切换账号时必须换人，回绕到自己是错误行为
        （否则单账号分组会"切到自己"并静默成功）。
        """
        with self._lock:
            total = len(self.slots)
            if total <= 1:
                return -1
            for offset in range(1, total):
                index = (after + offset) % total
                if self.slots[index].available:
                    return index
            return -1

    # ------------------------------------------------------------------ 状态更新

    def begin_work(self, index: int) -> None:
        with self._lock:
            slot = self.slots[index]
            slot.state = AccountState.WORKING
            slot.busy = True
            slot.last_used_at = now_local().isoformat()

    def end_work(self, index: int, *, succeeded: bool) -> None:
        with self._lock:
            slot = self.slots[index]
            slot.busy = False
            if succeeded:
                slot.consecutive_failures = 0
                slot.state = AccountState.ACTIVE
            else:
                slot.state = AccountState.ACTIVE if slot.available else slot.state

    def mark_fault(self, index: int, kind: FaultKind, *, note: str = "") -> AccountState:
        """记录故障；连续失败达到阈值或故障本身不可恢复时转为 FAILED。"""
        with self._lock:
            slot = self.slots[index]
            slot.last_fault = kind
            slot.last_fault_at = now_local().isoformat()
            slot.consecutive_failures += 1
            slot.busy = False
            if note:
                slot.notes = note

            hard_fault = kind in {FaultKind.QUOTA_EXHAUSTED, FaultKind.BANNED, FaultKind.AUTH_INVALID}
            if hard_fault or slot.consecutive_failures >= self._failure_threshold:
                slot.state = AccountState.FAILED
                _log.warning(
                    "group=%s index=%d label=%s 标记 FAILED（fault=%s 连续失败=%d）",
                    self.name,
                    slot.index,
                    slot.label,
                    kind.value,
                    slot.consecutive_failures,
                )
            else:
                slot.state = AccountState.ACTIVE
                _log.info(
                    "group=%s index=%d label=%s 记录故障 %s（未达阈值 %d）",
                    self.name,
                    slot.index,
                    slot.label,
                    kind.value,
                    self._failure_threshold,
                )
            return slot.state

    def deactivate_current(self, *, state: AccountState, note: str = "") -> int:
        """把当前激活账号置为 ``state``，并把激活指针移到下一个可用账号。"""
        with self._lock:
            previous = self._active_index
            if previous >= 0:
                slot = self.slots[previous]
                slot.state = state
                slot.busy = False
                if note:
                    slot.notes = note
            following = self.next_available(after=max(previous, 0))
            if following >= 0:
                self._activate_locked(following)
            else:
                self._active_index = -1
            return following

    def force_activate(self, index: int) -> int:
        """强制激活指定序号（供手动切换使用）。"""
        with self._lock:
            slot = self.slots[index]
            if not slot.available:
                # 手动指定时可恢复被 FAILED 的账号：人工介入即视为已处理。
                slot.state = AccountState.ACTIVE
                slot.consecutive_failures = 0
                slot.notes = "手动激活"
            self._activate_locked(index)
            return index

    # ------------------------------------------------------------------ 队列计数

    def set_queue(self, *, queued: int | None = None, running: int | None = None) -> None:
        with self._lock:
            if queued is not None:
                self._queued = queued
            if running is not None:
                self._running = running

    def status(self) -> GroupStatus:
        with self._lock:
            return GroupStatus(
                group=self.name,
                kind=self.kind,
                active_index=self._active_index,
                queued=self._queued,
                running=self._running,
                accounts=[slot.to_status(active_index=self._active_index) for slot in self.slots],
            )

    # ------------------------------------------------------------------ 持久化

    def dump(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_index": self._active_index,
                "accounts": [slot.to_json() for slot in self.slots],
            }

    def restore(self, payload: dict[str, Any]) -> None:
        """恢复上次运行的状态；账号数量或标签对不上时忽略该份状态。"""
        with self._lock:
            accounts = payload.get("accounts") or []
            if len(accounts) != len(self.slots):
                _log.info("group=%s 账号数量变化，忽略旧状态", self.name)
                return
            for index, raw in enumerate(accounts):
                if raw.get("index") != self.slots[index].index:
                    _log.info("group=%s 状态序号不匹配，忽略旧状态", self.name)
                    return
                self.slots[index].restore(raw)

            active = int(payload.get("active_index", -1))
            if 0 <= active < len(self.slots) and self.slots[active].available:
                self._activate_locked(active)
            else:
                self._activate_first_available()


class PoolManager:
    """全部分组的账号池容器。

    只暴露按分组操作的方法；**不存在"跨组共享锁"**，这是跨组并行的前提。
    """

    def __init__(self, accounts: AccountsConfig, *, failure_threshold: int = 2) -> None:
        self._accounts = accounts
        #: 分组顺序 = 配置文件里的声明顺序（内置三组在前，API 组按声明追加）。
        self._order: list[str] = list(accounts.groups)
        self._pools: dict[str, GroupPool] = {
            name: GroupPool(
                name,
                accounts.group(name).accounts,
                failure_threshold=failure_threshold,
                kind=accounts.group(name).kind,
            )
            for name in self._order
        }

    @property
    def accounts_config(self) -> AccountsConfig:
        return self._accounts

    def pool(self, group: str) -> GroupPool:
        try:
            return self._pools[group]
        except KeyError as exc:
            raise ToolError(
                ErrorCode.GROUP_NOT_FOUND,
                f"未配置分组：{group}",
                detail={"group": group, "available": sorted(self._pools)},
            ) from exc

    def groups(self) -> Iterator[GroupPool]:
        for name in self._order:
            yield self._pools[name]

    @property
    def kinds(self) -> dict[str, GroupKind]:
        """分组名 → 类型（供执行器分派）。"""
        return {name: self._accounts.group(name).kind for name in self._order}

    def statuses(self, *, only: str | None = None) -> list[GroupStatus]:
        if only is not None:
            return [self.pool(only).status()]
        return [pool.status() for pool in self.groups()]

    # ------------------------------------------------------------------ 持久化

    def dump(self) -> dict[str, Any]:
        return {"groups": {name: pool.dump() for name, pool in self._pools.items()}}

    def restore(self, payload: dict[str, Any]) -> None:
        groups = payload.get("groups") or {}
        for name, pool in self._pools.items():
            raw = groups.get(name)
            if isinstance(raw, dict):
                pool.restore(raw)

    def save(self, path: Path) -> None:
        """把状态写盘（原子替换），供重启后延续账号顺序与故障标记。"""
        data = self.dump()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名唯一：多线程可能同时保存状态，共用固定名会互删文件。
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log.warning("状态文件损坏，忽略：%s", path)
            return False
        if not isinstance(payload, dict):
            return False
        self.restore(payload)
        return True


__all__ = ["AccountSlot", "GroupPool", "PoolManager"]
