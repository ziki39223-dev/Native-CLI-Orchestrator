# 分层图 L0：总指挥 ↔ 调度 MCP ↔ 三组 CLI

> 状态：P1 · 本轮只落 L0。L1（账号轮换时序）见 `02-account-rotation.md`；L2–L4 按阶段补。

## L0-1 纵向依赖

```
[总指挥] DSH + FreeLLMAPI 免费聚合网关
   │  只做：拆任务、判流转、汇总；不消耗三家额度
   │  MCP：SSE（主推）/ stdio
   ▼
┌────────────────────────────────────────────────────────┐
│ [本项目] Native-CLI-Orchestrator                        │
│                                                        │
│  ① entry/    SSE 入口 │ stdio 入口                      │
│  ② server/   六工具注册 · 生命周期                       │
│  ③ tools/    信封 · 错误映射 · 校验                      │
│  ④ pools/    账号池状态机 · 调度器（组锁）                │
│  ⑤ runners/  MockRunner │ CliRunner                    │
│  ⑥ tasks/    manifest · inbox · .DONE                 │
│  ⑦ upstream/ 备份 · 同步 · 校验 · 回滚                  │
└──────┬──────────────────────────────┬─────────────────┘
       │ stdio MCP（每组建 1 个子进程） │ 文件协议
       ▼                              ▼
┌──────────────────┐        ┌──────────────────────────┐
│ trae_group       │        │ workspace/<task_id>/     │
│  ├ trae-a 在线    │        │   inbox/request.json     │
│  └ trae-b 休眠    │        │   artifacts/             │
│ qoder_group      │        │   stdout.log（脱敏）      │
│  ├ qoder-a 在线   │        │   .DONE / .FAILED        │
│  └ qoder-b 休眠   │        └──────────────────────────┘
│ workbuddy_group  │
│  └ wb-a 在线      │
└──────────────────┘
```

## L0-2 并行度

```
时间轴 →
trae_group   [任务A─────────────────]
qoder_group       [任务B────────]
workbuddy        [任务C──────────────]     ← 三组互不阻塞
（组内同时间仅 1 行：第二个任务必须等第一个结束）
```

## L0-3 横向支撑

```
contracts/      六工具模型 · 15 错误码 · 枚举      ← 被所有层依赖
config/         config.yaml + accounts.yml
common/         ID · 脱敏 · 重试
observability/  三本日志 · 故障识别
```

## 下一层

- L1 账号选择与故障切换时序：`02-account-rotation.md`（本轮同时完成）
