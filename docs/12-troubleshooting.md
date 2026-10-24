# 故障排查

> 状态：P2 · 按"症状 → 原因 → 处理"组织。优先跑 `scripts\doctor.bat`，多数问题它会直接指出来。

## 0. 先跑自检

```bat
scripts\doctor.bat          REM 基础检查
scripts\doctor.bat deep     REM 额外探测 API 地址连通性
```

它会检查：Python 版本、依赖、config.yaml、accounts.yml、每组 CLI 是否在 PATH、
每个 API key 是否设置、端口是否被占、工作目录是否可写、上游仓库是否配置。

**所有检查都是只读的，不会改你的配置，也不会打印 key 的值。**

## 1. 启动期问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `[启动失败] CONFIG_INVALID` | config.yaml 语法/字段错 | `python -m native_cli_orchestrator.entry sse --print-config` 看解析结果；按 `detail.reason` 修 |
| `[启动失败] CONFIG_INVALID: accounts 配置校验失败` | accounts.yml 结构错 | 自检会列出出错字段路径（`↳` 行） |
| `端口 8770 不可用` | 已被占用 | `scripts\start_sse.bat 8771`；或 `netstat -ano \| findstr 8770` 找进程 |
| `ModuleNotFoundError: native_cli_orchestrator` | 没安装 | `pip install -e .`；或确认在项目根执行 |
| `ModuleNotFoundError: mcp` | 依赖没装 | `scripts\setup.bat` |
| `DEPENDENCY_MISSING`（real 模式） | 缺 httpx2 等 | `pip install -r requirements.txt` |

## 2. CLI 组问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `CLI_NOT_FOUND`：找不到可执行文件 | CLI 没装 / 不在 PATH | `python devtools\probe_cli.py` 摸真实命令；改 `accounts.yml` 的 `cli` 字段 |
| `CLI_NOT_FOUND`：命令/参数错误 | 启动参数与默认不符 | 同上；探针报告会给出建议 `cli` |
| `ACCOUNT_NOT_READY`：环境变量未设置 | 凭据没配 | `setx` 后**重开终端** |
| `TASK_TIMEOUT`：超时未出 `.DONE` | ①CLI 不写标记 ②任务太大 | ①把该组设 `long_running` 相关语义（见下）②调大 `timeout_s` |
| 任务 `succeeded` 但产物为空 | CLI 把结果写在别处 | 查 `workspace/<task_id>/stdout.log`；让 CLI 把产物写进 `artifacts/` |
| 残留 CLI 进程 | 异常退出未清理 | 重启服务；`taskkill /IM traecli.exe /F` |

**CLI 不写 `.DONE` 怎么办**：说明它是"一次性 CLI"（跑完即退）。
这类 CLI 应让调度器以退出码判定，而不是等标记 —— 在 `runners/cli.py` 的 `PROFILES` 里
把该组的 `long_running` 设为 `False`（探针报告显示"MCP 握手 ⚠️"通常就是这种）。

## 3. API 组问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `ACCOUNT_NOT_READY`：没有可用 API key | env 里没有 `API_KEY`/`OPENAI_API_KEY` 一类变量 | 确认 `env:` 变量名与 `setx` 一致 |
| `POOL_EXHAUSTED`：预算已用尽 | 超 `daily_budget` | 提预算，或删 `workspace/.api_budget.json`（会当日清零） |
| `TASK_FAILED` + `context_too_long` | prompt 超模型上下文 | 缩短输入 / 分批 / 换大上下文模型（**换 key 无用**） |
| `TASK_FAILED` + `model_unavailable` | 模型名错或无权限 | 核对 `api.model`；OpenRouter 需带 `作者/` 前缀 |
| `TASK_FAILED` + `crashed`（5xx） | 上游节点故障 | 会自动换 key；全组都 5xx 说明是供应商问题 |
| HTTP 400 但看不出原因 | 某家字段支持不一 | 查 `workspace/<task_id>/inbox/request.json` 的实际请求体 |
| 本地 Ollama 连不上 | 服务没起 / 地址错 | `ollama serve`；`base_url: http://127.0.0.1:11434/v1` |
| 花费对不上 | 没填单价 | 填 `input/output_price_per_mtok`，否则 `cost_yuan` 恒 0 |

## 4. 调度与任务问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 任务一直 `queued` | 同组前一个任务没结束（组内串行） | 正常；查前一个任务的 `stdout.log` |
| `POOL_EXHAUSTED`（全组无可用） | 所有账号 FAILED/DISABLED | 补额度后 `switch_account(target_index=0)` 或改配置 `reload_config` |
| `TASK_NOT_FOUND` | task_id 错 / 工作区被清 | 核对 ID；`workspace/` 目录是否还在 |
| 想让某账号暂时不参与 | — | `accounts.yml` 里加 `disabled: true` 后 `reload_config` |
| 改了配置没生效 | 没重载 | `reload_config`（有任务在跑时会拒绝，等它结束） |
| 重启后账号状态没重置 | 状态持久化在 `.pools.state.json` | 删 `workspace/.pools.state.json` 后重启 |

## 5. 上层工作台（DSH / Cherry）连不上

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 看不到工具 | 连了 `/messages/` | 客户端应连 `http://<host>:8770/sse` |
| 跨机连不上 | 只监听 127.0.0.1 | `config.yaml` 改 `host: 0.0.0.0`，放行防火墙 |
| 401 / 连接被拒 | 设了 `auth_token` 但没带 | 请求头加 `Authorization: Bearer <token>` |
| 连上后没响应 | 服务被 Ctrl+C | 看 `logs/orchestrator.log` 最后几行 |

## 6. 日志在哪

| 文件 | 内容 |
| --- | --- |
| `logs/scheduler.log` | 选账号、切换、任务状态变化 |
| `logs/cli.log` | 子进程启停、CLI 输出摘要、API 用量与花费（**已脱敏**） |
| `logs/upstream.log` | 上游同步与回滚 |
| `logs/orchestrator.log` | 全量（含 stderr） |
| `workspace/<task_id>/` | 单个任务的现场：manifest / 请求 / 输出 / 产物 / 标记 |

## 7. 上报问题时请附上

1. `scripts\doctor.bat` 的完整输出（已脱敏，可安全粘贴）
2. 相关 `task_id` 与 `logs/` 对应时段
3. 若是 API 组：`fault_kind` 与 `error_code` 字段值

**不要**粘贴 `config/accounts.yml` 里的真实凭据（本项目已设计成不含凭据，但请确认）。
