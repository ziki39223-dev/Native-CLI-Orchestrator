# 开发规则

> 状态：P1 · 违反以下任一条的提交直接打回。

## 1. 契约纪律（最高优先级）

1. `contracts/` + `docs/06-tool-contract.md` 已冻结为 **v1.0.0**：工具名、字段名、错误码**只增不改不删**。
2. 变更流程：改 `01` → 升次版本号（可加字段）/ 主版本号（破坏性）→ 同步 `06` → 适配 Mock → 通知编排侧。
3. 出参一律结构化信封 `{ok, error_code, message, backend, correlation_id, elapsed_ms, error, data}`；
   禁止裸字符串、禁止抛裸异常到 MCP 层。

## 2. 三条不变量的代码纪律

| 不变量 | 必须 | 禁止 |
| --- | --- | --- |
| 组内串行 | `Scheduler._execute` 全程持 `pool.lock` | 在锁外调用 `runner.run` |
| 组内单实例 | 只有 `active_index` 可 ACTIVE/WORKING；任务结束杀进程 | `force_activate` 时不把旧的 WORKING 保留（除非 busy） |
| 跨组并行 | 每组建自己的 `RLock` | 引入任何全局锁 / 全局串行队列 |

`next_available()` **不得回绕到自身**：切换账号必须换人，否则单账号分组会"切到自己"并静默成功。

## 3. Mock / Real 隔离

1. Mock 是**一等公民**：开发机 `pytest` 全绿才能合并。
2. Real 专属代码（`subprocess`、`taskkill`）只允许出现在 `runners/cli.py`。
3. Real 失败必须有错误码，禁止 `except: pass`；环境类错误（命令不存在/参数错误）**不切账号**，直接报 `CLI_NOT_FOUND`。
4. `MockRunner.requires_credentials = False`：开发机没有任何环境变量也能跑通全流程（这是硬需求）。

## 3.1 一次性 CLI 与常驻 CLI

`CliProfile.long_running` 决定"没有 `.DONE` 时怎么判"：

| 值 | 语义 | 判定 |
| --- | --- | --- |
| `True`（默认） | MCP server 常驻，靠 `.DONE` 报完成 | 退出码 0 但无 `.DONE` = **失败**（ADR-004） |
| `False` | 一次性 CLI，跑完即退 | 退出码 0 = 成功，不需要 `.DONE` |

探针报告如果显示某家"未观察到 MCP 握手"，通常说明它是纯 CLI → 把该组设 `long_running: False`，
并在 `prompt` 里要求它直接输出结果到 `artifacts/`。

## 3.2 API 组的额外纪律

1. **绝不把 intercept 到的真实 key 写进任何落盘文件**：`inbox/request.json` 只记
   `protocol/model/max_tokens/temperature`，不记 header；`stdout.log` 也过 `redact()`。
2. **只在"key 不能用了"时换 key**：`context_too_long` / `model_unavailable` 必须
   `retry_with_other_account=False`，否则会把整组好 key 标废而额度一分没用完。
3. **预算是硬护栏**：`_check_budget` 必须在**发请求之前**执行，不能"先花钱再报错"。
4. **账本必须落盘**：`workspace/.api_budget.json`，重启不清零（否则护栏形同虚设）。
5. **价格未知时填 0**：只统计 token 不算钱，禁止瞎猜单价。

## 4. 安全口径（凭据）

1. `accounts.yml` 只写 `env:变量名`；带 `token/key/secret/password/cookie` 字样的键，值不以 `env:` 开头即**校验失败**。
2. 以下位置的数据必须先过 `common.text.redact()`：
   - `stdout.log` / `stderr.log`
   - 失败原因 `fail_reason` / `RunResult.reason`
   - 日志里的 CLI 输出
3. 出参只出现账号 `label` 与 `index`，**永不出现** env 实际值、token、cookie。
4. 契约测试 `test_pool_status_never_leaks_credentials` 会扫出参全文，泄露即红。

## 5. 错误码与日志

错误码（冻结，15 个）：`OK` / `INVALID_ARGUMENT` / `GROUP_NOT_FOUND` / `TASK_NOT_FOUND` / `POOL_EXHAUSTED` /
`ACCOUNT_NOT_READY` / `CLI_NOT_FOUND` / `PROCESS_START_FAILED` / `TASK_TIMEOUT` / `TASK_FAILED` /
`CONFIG_INVALID` / `UPSTREAM_UPDATE_FAILED` / `UPSTREAM_INCOMPATIBLE` / `DEPENDENCY_MISSING` / `INTERNAL_ERROR`。

三本日志（`observability/logging.py`）：

| 文件 | logger | 内容 |
| --- | --- | --- |
| `scheduler.log` | `dao.scheduler` | 选号、切换、队列、任务状态变化 |
| `cli.log` | `dao.cli` | 子进程启停、输出摘要（脱敏） |
| `upstream.log` | `dao.upstream` | 更新、备份、回滚 |
| `orchestrator.log` | root | 全量（stderr 同步） |

纪律：
- 禁止 `print`；stdio 模式**禁止**任何写 stdout 的 handler（stdout 是协议通道）。
- `queued`/`running`/`timeout` 是正常状态，用 INFO；只有真失败才 WARNING/ERROR。
- 日志只打 prompt 长度 + 前 60 字摘要，不打全文。

## 6. 代码风格

- Python 3.12，全量类型注解；`ruff`（含 `S`、`BLE`、`PYI`）+ `mypy --strict` 必须全绿。
- 中文注释只在"为什么"层，不复述代码。
- 一文件一职责，超 ~300 行拆分（`scheduler.py` 已接近上限，再加功能先拆）。
- 常量（分组名、标记文件名、超时上限）只从 `contracts/constants.py` 取。

## 7. 测试门禁

| 层 | 内容 | GUI/CLI 依赖 | 状态 |
| --- | --- | --- | --- |
| `tests/unit` | 状态机、调度、工作区协议、上游回滚、环境级错误、API 调用与预算、分组类型 | 无 | 114 项 |
| `tests/contract` | 六工具入参出参、错误映射、凭据不外泄 | 无 | 20 项 |
| `tests/e2e` | ① MCP 客户端闭环 5 项；② **CliRunner 真子进程链路** 16 项（用 `tests/stubs/stub_cli.py`） | 无 | 21 项 |

- 命令：`.venv\Scripts\python -m pytest -q`（当前 155 项全绿，约 24s）。
- 新增工具/字段必须同时加契约测试 + e2e。
- 涉及不变量的改动必须能通过 `test_pool_state_machine.py`。

## 8. 版本与提交

1. 语义化版本；契约破坏性变更升 MAJOR。
2. 提交信息 `<scope>: <what>`，scope 用目录名（`pools` / `runners` / `contracts` / `docs`）。
3. 文档与代码同步改动放同一提交。
4. 打包前：`pytest` 全绿 → `ruff`+`mypy` 全绿 → 配置模板齐全 → README 与 `09` 步骤一致。

## 9. 交付卫生

包里**不得**含：`workspace/` 任务目录、`backups/`、`logs/`、`.pools.state.json`、`.venv/`、`__pycache__/`、
本机绝对路径、真实凭据。
