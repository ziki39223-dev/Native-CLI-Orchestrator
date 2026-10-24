# Native-CLI-Orchestrator（三家 CLI 账号池聚合调度 MCP 服务）

> 一句话调度逻辑：**各分组互相并行；同一组内串行，同一时刻只激活 1 个账号（1 个 CLI 进程 / 1 个 API key），
> 其余备用账号休眠不登录、不发请求，轮到了才拉起。**
>
> 分组有两类：`kind: cli`（三家客户端，吃免费额度）与 `kind: api`（**任意 OpenAI 兼容供应商**，
> DSH 自己充值的 key、FreeLLMAPI 之类免费网关、OpenRouter 聚合、本地 Ollama/vLLM 都能接）。

上层总指挥 DSH 对接免费 API 聚合网关只做轻量调度判断；真正干活的推理额度由三家 CLI 的免费额度承担。
本包只含**聚合调度 MCP 服务**，不含 DSH / FreeLLMAPI 的配置。

**当前状态：阶段 1 完成**（脚手架 + 契约冻结 + Mock 闭环）。工具契约已冻结为 v1.0.0。

## 为什么是 CLI 而不是 GUI

| 维度 | 本项目（CLI 后台进程） | 早期 GUI 方案 |
| --- | --- | --- |
| 前台依赖 | 无窗口、锁屏可跑 | 需要窗口可捕获 |
| 稳定性 | 进程级通信，MCP 协议 | UIA 控件随客户端版本漂移 |
| 多账号 | 进程隔离，切换即换进程 | 同一窗口切账号困难 |
| 风控 | 同组只 1 个进程在线 | 多实例易被识别 |

## 三条不变量（改动代码前请先读）

1. **组内串行**：同一分组同一时刻只有一个任务在跑（持组锁）。
2. **组内单实例**：只有 `active_index` 指向的账号在线，其余账号状态恒为 `dormant`（不拉起进程、不登录、不发请求）。
3. **跨组并行**：不同分组各持自己的锁，Trae 在跑不影响 Qoder 取号。

单元测试直接对这一组不变量做断言（`tests/unit/test_pool_state_machine.py`），改坏了会红。

## 六个对外工具

| 工具 | 作用 |
| --- | --- |
| `submit_task(group, task_manifest, timeout_s?, wait?)` | 提交任务到任意分组（cli 或 api），默认异步 |
| `get_task_result(task_id, wait_s?)` | 取状态/产物；`queued`/`running` 属正常 |
| `get_pool_status(group?)` | 查看各账号在线/休眠/故障与排队数 |
| `switch_account(group, target_index?, reason?)` | 手动强制切下一个或指定账号 |
| `reload_config(path?)` | 热重载 `accounts.yml`（不重启服务） |
| `update_upstream(check_only?, force?)` | 上游同步，带备份与失败回滚 |

API 分组额外可见：`get_task_result` 带 `prompt_tokens` / `completion_tokens` / `cost_yuan`，
`get_pool_status` 带 `kind` 与 `api_budget`（当日花费、剩余预算）。

契约细节见 [docs/06-tool-contract.md](docs/06-tool-contract.md)（**已冻结**）。

## 快速开始

**第一次用请直接看 [START_HERE.md](START_HERE.md)**（3 步：双击安装 → 配账号 → 改 real 启动）。

```bat
scripts\setup.bat        REM 一键安装 + 自动自检
scripts\doctor.bat       REM 随时再查一遍环境
scripts\start_sse.bat    REM 启动（默认 8770 端口）
```

其他常用命令：

```bat
REM 补齐缺失的配置文件（不覆盖已有）
python -m native_cli_orchestrator.entry --init

REM 自检（--deep 额外探测 API 地址连通性）
python -m native_cli_orchestrator.entry --doctor --deep

REM 用自带客户端验证闭环（需服务已启动）
.venv\Scripts\python scripts\mcp_client_demo.py sse --switch

REM 跑全部测试（178 项，含真实子进程链路 + API 调用，零真实 CLI/key 依赖）
.venv\Scripts\python -m pytest -q
```

## 接任意家 API（不需要三家 CLI 也能用）

```yaml
# config/accounts.yml
deepseek_group:
  kind: api
  api:
    base_url: https://api.deepseek.com/v1   # 换成任意 OpenAI 兼容地址
    model: deepseek-chat
    daily_budget: 20            # 日预算（元），超了当天拒单；0=不限额
    input_price_per_mtok: 1.0   # 用于估算花费
    output_price_per_mtok: 2.0
  accounts:
    - label: ds-a
      env:
        OPENAI_API_KEY: env:DEEPSEEK_KEY_1
```

`setx DEEPSEEK_KEY_1 "sk-..."` → `reload_config` → 用 `submit_task(group="deepseek_group", ...)`。
供应商速查与排错见 [docs/15-api-providers.md](docs/15-api-providers.md)。

## 工作机部署（real 模式）

0. **先跑探针**：`python devtools/probe_cli.py`，一条命令摸清三家 CLI 的真实命令/参数/是否说 MCP，
   报告写到 `probe-report/`（含可直接粘贴的 `accounts.yml` 片段）。
1. 装三家 CLI，确认 `traecli mcp-server` / `qoder mcp serve` / `workbuddy-mcp` 可执行。
2. 录账号凭据到系统环境变量（**不要**写进配置文件）：`TRAE_TOKEN_1`、`QODER_TOKEN_1` …
3. 编辑 `config/accounts.yml`：填 `env:变量名`、按优先级排好组内顺序。
4. `config.yaml` 改 `mode: real`。
5. `scripts\start_sse.bat`，用 `scripts\mcp_client_demo.py` 或 MCP Inspector 冒烟。

完整步骤见 [docs/09-deploy-and-packaging.md](docs/09-deploy-and-packaging.md)。

## 文档索引

| 文档 | 作用 |
| --- | --- |
| [START_HERE.md](START_HERE.md) | **从这开始**：3 步上手（安装 → 配账号 → 启动） |
| [docs/00-doc-map.md](docs/00-doc-map.md) | 文档体系：清单、职责、编写顺序、状态 |
| [docs/01-requirements-baseline.md](docs/01-requirements-baseline.md) | 需求基线（定稿版）：目标、边界、约束、验收 |
| [docs/02-architecture.md](docs/02-architecture.md) | 架构：分层、依赖方向、运行模式 |
| [docs/03-directory-layout.md](docs/03-directory-layout.md) | 目录分区与文件归属 |
| [docs/04-dev-rules.md](docs/04-dev-rules.md) | 开发规则：契约冻结、状态机纪律、安全口径 |
| [docs/05-dev-plan.md](docs/05-dev-plan.md) | 开发计划：阶段、任务、DoD、风险 |
| [docs/06-tool-contract.md](docs/06-tool-contract.md) | 工具契约 v1.1.0（已冻结） |
| [docs/07-decision-log.md](docs/07-decision-log.md) | 决策记录（ADR）与待定项 |
| [docs/09-deploy-and-packaging.md](docs/09-deploy-and-packaging.md) | 打包、迁移、工作机适配 |
| [docs/10-mcp-inspector-testing.md](docs/10-mcp-inspector-testing.md) | 联调手册：Inspector / demo 客户端 / 异常路径 |
| [docs/11-integration-dsh.md](docs/11-integration-dsh.md) | DSH 接入指引 + FreeLLMAPI 说明 |
| [docs/12-troubleshooting.md](docs/12-troubleshooting.md) | 故障排查：按症状查原因与处理 |
| [docs/14-cli-probe.md](docs/14-cli-probe.md) | 三家 CLI 探针使用说明 |
| [docs/15-api-providers.md](docs/15-api-providers.md) | 接任意家 OpenAI 兼容 API：配置、供应商、预算、排错 |
| [docs/diagrams/01-layers.md](docs/diagrams/01-layers.md) | 分层图 L0 |
| [docs/diagrams/06-api-runners.md](docs/diagrams/06-api-runners.md) | 分层图 L5：API 组分派、错误翻译、预算 |

## 边界

| 属于本项目 | 不属于本项目 |
| --- | --- |
| 账号池状态机、轮换调度、子进程管理、任务文件协议、上游同步、MCP 工具 | DSH 的提示词 / 流水线 DAG / 人工审批节点、FreeLLMAPI 节点清单、三家 CLI 本体 |
