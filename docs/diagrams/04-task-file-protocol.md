# 分层图 L3：任务文件协议

> 状态：P2 · 本轮只落 L3（一个任务目录里各文件的写入顺序、原子性与读取方）。
> 上一层 L2 见 `03-process-lifecycle.md`；下一层 L4（上游更新与回滚）在下一轮。

## L3-1 目录结构与写入时序

```
workspace/<task_id>/
 ├─ manifest.json        ← ① 调度器写（提交时，之后视为不可变）
 ├─ prompt.md            ← ② 调度器写（渲染后的完整指令）
 ├─ .task.json           ← ③ 调度器写（内部状态，随状态变化反复更新）
 ├─ inbox/request.json   ← ④ 调度器写（每次尝试前，含 attempt 序号）
 ├─ stdout.log           ← ⑤ CLI 输出泵追加（脱敏后）
 ├─ stderr.log           ← ⑤ CLI 输出泵追加（脱敏后）
 ├─ artifacts/           ← ⑥ CLI 写产物
 ├─ outline.md           ← ⑥ CLI 也可能直接写在任务根
 ├─ .DONE                ← ⑦ CLI 写（成功信号）
 ├─ .FAILED              ← ⑦ CLI 写（失败信号，与 .DONE 互斥）
 └─ result.json          ← ⑧ 调度器写（终态汇总，给上层读）
```

写入顺序不可颠倒：**先 ①②③ 才 ④**（CLI 读 request 时需要 manifest 已存在），
**先 ⑦ 才 ⑧**（调度器看到标记才写结果）。

## L3-2 原子写（防并发撕裂）

所有 JSON 写入走 `_write_json`：

```
写 path.<pid>.<tid>.<rand8>.tmp
        ↓
os.replace(tmp, path)     ← POSIX / Windows 都是原子替换
```

为什么临时文件名要带 pid+线程 id+随机：
早期版本用固定名 `path.tmp`，跨组并行时两个线程互相覆盖/删除，
Windows 上抛 `FileNotFoundError`。测试连跑多次才复现，属于典型并发缺陷。

## L3-3 `.DONE` / `.FAILED` 互斥

```
mark_done(summary)         mark_failed(reason)
   ├─ 删 .FAILED              ├─ 删 .DONE
   └─ 写 .DONE                └─ 写 .FAILED
```

判定时**先查 `.DONE`**（成功优先）。理由：CLI 可能在失败后又补写了成功标记（重试成功），
此时以成功为准更符合预期。

## L3-4 产物收集规则

```
collect_artifacts(expected=[...])
  ├─ artifacts/ 下全部文件（递归）
  ├─ 任务根下**非协议文件**（排除 manifest.json / .task.json / result.json /
  │   prompt.md / stdout.log / stderr.log / request.json / .DONE / .FAILED）
  └─ expected 里没生成的项也列出来（size_bytes=0, sha256=""）
```

最后一条是刻意设计：DSH 需要知道"该有但没有"的产物，而不是静默少一项。

产物字段：`{path（相对路径，正斜杠）, size_bytes, sha256}`。

## L3-5 状态文件 `.task.json` 的生命周期

```
submitted  → QUEUED   （含预定账号）
执行开始    → RUNNING  （含实际账号、attempt 计数）
终态        → SUCCEEDED / FAILED（含 elapsed_s、fault_kind、error_code）
```

服务重启后 `get_task_result` 靠它恢复（`Scheduler._load_record_from_disk`）：
即使内存表空了，只要目录还在就能读回历史结果。

`restore` 的防御：`WORKING` 一律回落为 `ACTIVE` —— 重启后不可能还有子进程在跑。

## L3-6 协议契约（CLI 侧必须遵守的三条）

写在 `prompt.md` 与 `request.json` 里，CLI 实现方（或适配脚本）需要：

1. 读 `inbox/request.json`（或 `prompt.md`）获取任务；
2. 产物写入 `artifacts/`（或任务根）；
3. 完成后写 `.DONE`，无法完成则写 `.FAILED` 并说明原因。

若某家 CLI 无法遵守（纯一次性 CLI）→ 用 `long_running: False` 让调度器以退出码判定。

## L3-7 已被测试覆盖

| 行为 | 测试 |
| --- | --- |
| 目录骨架创建、幂等 | `test_create_makes_all_directories`、`test_create_is_idempotent` |
| manifest 往返、缺失、损坏 | `test_manifest_roundtrip` / `test_missing_manifest_raises` / `test_corrupt_manifest_raises` |
| DONE/FAILED 互斥 | `test_done_and_failed_are_mutually_exclusive` |
| 输出脱敏落盘 | `test_output_is_redacted` |
| 产物两处收集 + 协议文件排除 | `test_collect_artifacts_from_dir_and_root` |
| 缺失期望产物也列出 | `test_collect_artifacts_lists_missing_expected` |
| sha256 稳定 | `test_artifact_sha256_stable` |
| 原子写不留 .tmp | `test_state_write_is_atomic` |

## 下一层

- L4 上游更新与回滚（备份 → 同步 → 校验 → 回滚的目录级操作）：下一轮写 `05-upstream-rollback.md`
