# DSH 接入指引 + FreeLLMAPI 配置说明

> 状态：P1 起草 · P4 定稿。**本文只写"怎么接"，不写 DSH 的提示词/DAG 内容**（那属于编排侧资产）。

## 1. 接入拓扑

```
DSH（总指挥）
 ├─ 模型：指向 FreeLLMAPI 免费聚合网关（OpenAI 兼容）
 └─ MCP：SSE → http://<工作机IP>:8770/sse
        （stdio 兜底：DSH 原生本地 MCP，子进程方式）
```

DSH 只需要知道**三个分组名**和**六个工具**，不需要知道下面有几个账号、当前哪个在线。

## 2. 添加 MCP 服务

### 方式 A：SSE（推荐，方便换工作台）

1. 工作机启动：`scripts\start_sse.bat 8770 real`（局域网用先在 `config.yaml` 改 `host: 0.0.0.0`）。
2. DSH 里新增 MCP 服务，类型 **SSE**，URL：`http://127.0.0.1:8770/sse`（跨机填工作机内网 IP）。
3. 若设了 `server.auth_token`，请求头加 `Authorization: Bearer <token>`。
4. 连接成功后应能看到六个工具。

### 方式 B：stdio（工作台只支持本地子进程时）

```
Command:   python
Arguments: -m native_cli_orchestrator.entry stdio --config config/config.yaml
WorkingDir: <项目根>
```

## 3. DSH 侧的调用约定（三条）

1. **只传分组名**：`trae_group`（生成/架构）、`qoder_group`（评审/可行性）、`workbuddy_group`（文献/润色）。
2. **异步提交 + 轮询**：`submit_task(wait=false)` → 每 3–5 秒 `get_task_result(task_id)`，
   看到 `state=succeeded` 取 `artifacts`；`state=failed` 看 `fail_reason`。
   **不要因为 `state=queued/running` 就重试提交**（会重复消耗额度）。
3. **不要并提交同组任务**：同组本来就是串行，提前提交只会排队。需要并行就用不同分组。

## 4. 建议的三角色分工（说明性质，具体提示词在 DSH 内维护）

| 角色 | 分组 | 适合的任务 |
| --- | --- | --- |
| Trae 专家 | `trae_group` | 架构设计、开题文档、规范校验、代码生成 |
| Qoder 专家 | `qoder_group` | 代码评审、仓库解读、技术可行性审查 |
| WorkBuddy 专家 | `workbuddy_group` | 文献阅读、综述撰写、文本润色、参考文献整理 |

典型流水线（DAG 在 DSH 内配置）：Trae 出大纲 → WorkBuddy 出综述 → Qoder 出评审 → Trae 汇总校验。
节点之间通过 `get_task_result` 拿到的 `artifacts` 路径串联。

## 5. FreeLLMAPI 说明

| 项 | 说明 |
| --- | --- |
| 作用 | 给 DSH 提供免费/低成本推理，承担"拆任务、判流转、写提示词"这类轻量调用 |
| 为什么不用三家额度 | 三家额度要留给真正的重活；总指挥如果烧三家额度，成本失控 |
| 本项目要做的事 | **无**。本服务不调用 FreeLLMAPI，只在 `get_task_result` 里返回产物供 DSH 汇总 |
| 节点列表更新 | 属 FreeLLMAPI 侧能力，与本项目无关（`update_upstream` 更新的是 CLI/schema，不是 API 节点） |

对应关系：

```
DSH ──(轻量判断)──► FreeLLMAPI ──► 免费模型
 │
 └──(重活)──► 本 MCP ──► 三家 CLI 免费额度
```

## 6. 全链路测试清单（P4）

- [ ] DSH 能列出六个工具，`get_pool_status` 三个分组都 `healthy=true`
- [ ] 正常任务：提交 → 轮询 → `succeeded` → 产物可读
- [ ] 额度耗尽：故障分组自动切号 → 任务仍成功（`account_attempts=2`）
- [ ] 全组耗尽：任务 `failed`，DSH 告警并暂停流水线（不自动无限重试）
- [ ] 上游更新后：`update_upstream --check` 无变更，任务仍能跑
- [ ] 人工审批节点：暂停后恢复，`get_task_result` 能继续读到结果
- [ ] 产物归档：DSH 把 `artifacts` 写成 md（归档逻辑在 DSH 侧）

## 7. 常见接入问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| DSH 看不到工具 | 连了 `/messages/` | 客户端连 `/sse` |
| 跨机连不上 | `host` 还是 `127.0.0.1` | 改 `0.0.0.0` + 放行防火墙 |
| 任务一直 queued | 同组前一个任务没结束 | 正常串行，等；查前一个 `stdout.log` |
| 全部账号 failed | 额度耗尽或之前故障注入 | `get_pool_status` 看 notes，补额度后 `switch_account(target_index=...)` |
| DSH 报"服务未响应" | 服务被 Ctrl+C 停了 | 重启 `start_sse.bat`，检查 `logs/orchestrator.log` |
