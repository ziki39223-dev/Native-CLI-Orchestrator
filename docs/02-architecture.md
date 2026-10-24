# 架构说明

> 状态：P1 · 已完成 L0 与 L1。L2–L4 见 `00-doc-map.md`，按阶段逐层补。

## 1. 一句话架构

DSH 通过 MCP（SSE / stdio）只调**一个**聚合调度服务；该服务内部按分组维护账号池状态机，
每个分组同一时刻只拉起一个 CLI 子进程（MCP over stdio），任务通过 inbox/manifest/.DONE 文件协议交付。

## 2. L0：分层与依赖方向

```
┌──────────────────────────────────────────────────────────────┐
│ 总指挥：DSH + FreeLLMAPI（本项目之外）                        │
│   只做任务拆解与流转判断；不消耗三家额度                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ MCP：SSE（主推）/ stdio
┌───────────────────────────▼──────────────────────────────────┐
│ 本项目：Native-CLI-Orchestrator                               │
│                                                              │
│  ① entry/      SSE 入口 · stdio 入口                          │
│  ② server/     MCPServer 装配 · 六工具注册 · 生命周期           │
│  ③ tools/      六工具：入参校验 → 调度器 → 信封 → 错误映射      │
│  ④ pools/      账号池状态机（GroupPool）+ 调度器（Scheduler）    │
│  ⑤ runners/    Runner 接口 ├─ MockRunner（开发机）              │
│                          ├─ CliRunner（CLI 组，子进程）          │
│                          ├─ ApiRunner（API 组，OpenAI 兼容 HTTP）│
│                          └─ CompositeRunner（按 kind 分派）      │
│  ⑥ tasks/      workspace 协议（manifest/inbox/.DONE/产物）     │
│  ⑦ upstream/   拉取、备份、校验、回滚                          │
│  ⑧ contracts/ 工具契约（唯一真源，已冻结）                      │
│  ⑨ config/     config.yaml + accounts.yml                     │
│  ⑩ observability/ 三本日志 + 故障识别                          │
└───────────┬───────────────────────────────┬──────────────────┘
            │ stdio MCP                     │ 文件协议
┌───────────▼───────────┐   ┌───────────────▼──────────────────┐
│ Trae 组（1 个在线）     │   │ workspace/<task_id>/             │
│ Qoder 组（1 个在线）    │   │   inbox/request.json             │
│ WorkBuddy 组（1 个在线）│   │   artifacts/  .DONE / .FAILED    │
└───────────────────────┘   └──────────────────────────────────┘
```

依赖方向（硬规则）：`entry → server → tools → pools → runners/tasks/upstream`；
`contracts` / `config` / `common` / `observability` 是被依赖方，任何层不得反向 import。

## 3. 三条不变量

| 不变量 | 落地机制 | 守护测试 |
| --- | --- | --- |
| 组内串行 | 每个 `GroupPool` 持一把 `RLock`，`_execute` 全程持锁 | `test_same_group_tasks_are_serialized` |
| 组内单实例 | 只有 `active_index` 为 ACTIVE/WORKING，其余强制 DORMANT；任务结束即杀进程 | `test_activate_keeps_exactly_one_online`、`test_concurrent_acquire_*` |
| 跨组并行 | 每组独立锁，不存在全局锁 | `test_cross_group_work_parallel` |

三条不变量对 `kind: api` 分组**同样成立**（"休眠"= 不发请求，而非不拉进程）。

## 4. 运行模式矩阵

| 维度 | mock（开发机） | real（工作机） |
| --- | --- | --- |
| Runner | `MockRunner`（脚本化成功/失败） | `CompositeRunner`（CLI→`CliRunner`，API→`ApiRunner`） |
| 凭据 | 不校验（`requires_credentials=False`） | 必须由环境变量提供 |
| 进程 | 无 | `CREATE_NO_WINDOW` + 独立进程组 |
| 完成信号 | 直接写 `.DONE` | 轮询 `.DONE`，进程退出也兜底 |
| 调度逻辑 | **完全相同** | **完全相同** |

## 5. 关键设计取舍（详见 `07-decision-log.md`）

1. **Runner 抽象**：Mock 与 Real 只换执行器，调度、状态机、任务协议全复用 —— 开发机测试才有意义。
2. **文件协议而非 IPC**：产物天然落盘，DSH 可直接读文件；异常时现场完整（manifest + 日志 + 标记）。
3. **进程即账号**：切换账号 = 换进程，天然隔离会话与凭据，也天然满足"同组只 1 个在线"。
4. **故障识别外置**：关键词表在 `observability/faults.py`，随上游文案更新，不动契约。
5. **未完成不是错误**：`queued`/`running` 走 `ok=true`，避免编排侧把正常等待当失败重试（白烧额度）。
6. **凭据只从环境变量**：配置写 `env:VAR`，日志/输出统一 `redact()`。

## 6. 后续分层

- L2 子进程生命周期（拉起 → 监督 → 终止/清理）：`diagrams/03-process-lifecycle.md`（P2）
- L3 任务文件协议细节：`diagrams/04-task-file-protocol.md`（P2）
- L4 上游更新与回滚：`diagrams/05-upstream-rollback.md`（P2）
