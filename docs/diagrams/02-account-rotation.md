# 分层图 L1：一次任务的账号选择与故障切换时序

> 状态：P1 · 本轮落 L1。上一层 L0 见 `01-layers.md`；下一层 L2（子进程生命周期）在 P2。

## L1-1 正常路径（无故障）

```
DSH            tools/            Scheduler         GroupPool        Runner
 │ submit_task ─►│                   │                 │              │
 │               │ 校验→submit ─────►│                 │              │
 │               │                   │ 建 task_id/工作区│              │
 │               │                   │ 记 QUEUED ──────►│ queued+1     │
 │◄─ task_id ────│◄── 立即返回 ──────│                 │              │
 │               │                   │                 │              │
 │               │      （后台线程）  │ with pool.lock ─►│              │
 │               │                   │ acquire() ─────►│ 返回 active  │
 │               │                   │ begin_work ────►│ WORKING      │
 │               │                   │ run(context) ───┼─────────────►│ 起进程
 │               │                   │                 │              │ 轮询 .DONE
 │               │                   │◄── succeeded ───┼──────────────│
 │               │                   │ end_work(ok) ──►│ ACTIVE       │
 │               │                   │ 写 result.json  │              │
 │ get_task_result ►│ ────────────►  │ state=succeeded │ 杀进程        │
 │◄─ artifacts ──│◄──────────────────│                 │              │
```

## L1-2 额度耗尽 → 自动换号

```
Scheduler                    GroupPool(trae)              Runner
  │ acquire() ───────────────►│ active=trae-a                │
  │ begin_work(0) ───────────►│ trae-a: WORKING              │
  │ run() ─────────────────────┼────────────────────────────►│ 起进程
  │◄──── fault=QUOTA_EXHAUSTED ┼─────────────────────────────│ 输出含 quota
  │ end_work(0, ok=False) ───►│ trae-a: ACTIVE               │
  │ mark_fault(0, QUOTA) ────►│ trae-a: FAILED（硬故障即刻）  │
  │ deactivate_current() ────►│ active=trae-b, trae-a 保持 FAILED，其余 DORMANT
  │ switch_cooldown（防高频）   │                              │
  │ acquire() ───────────────►│ active=trae-b                │
  │ begin_work(1) ───────────►│ trae-b: WORKING              │
  │ run() ─────────────────────┼────────────────────────────►│ 起新进程
  │◄──── succeeded ────────────┼─────────────────────────────│
  │ end_work(1, ok=True) ────►│ trae-b: ACTIVE               │
```

关键点：
- **同一时刻只有一个 "在线"（ACTIVE/WORKING）**：换号瞬间 A 变 FAILED、B 变 ACTIVE，不存在两个都在线的中间态。
- **换号 = 换进程**：A 的进程在 `Runner._terminate` 里被 `taskkill /T /F` 收掉。
- `account_attempts` 递增，最终写进 `get_task_result`，DSH 能看到"这次换过号"。

## L1-3 全部账号耗尽

```
attempt=1 trae-a → FAILED
attempt=2 trae-b → FAILED
attempt=3 trae-c → FAILED
        │
        ├─ acquire() 抛 POOL_EXHAUSTED（active_index = -1）
        └─ 任务 state=FAILED, fault_kind=quota_exhausted, account_attempts=3
           get_pool_status → healthy=false, warnings=["trae_group 无可用账号，需人工处理"]
```

此时 DSH 应暂停该分组的流水线并告警；恢复方式：
`switch_account(group, target_index=0)`（人工确认额度已恢复）或补新账号后 `reload_config`。

## L1-4 组内串行 vs 跨组并行

```
T0  submit(T, "任务1")  → 占组锁，开始执行
T0  submit(T, "任务2")  → QUEUED（线程已起，但卡在 pool.lock）
T0  submit(Q, "任务3")  → 立刻执行（Q 组另一把锁）

T1  "任务1" 结束 → 释放 T 组锁
T1  "任务2" 自动进入执行
```

不会出现"任务2 因为任务1 用了 trae-a 而换到 trae-b"——串行的目的就是复用同一个在线账号。

## 下一层

- L2 子进程生命周期（拉起参数、环境注入、输出泵、终止与僵尸清理）：P2 落 `03-process-lifecycle.md`
