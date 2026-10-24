# 联调手册（MCP Inspector + 自带客户端）

> 状态：P1 · 覆盖 Mock 全流程。real 模式步骤见 `09-deploy-and-packaging.md`。

## 1. 三种联调方式

| 方式 | 命令 | 适合 |
| --- | --- | --- |
| 自带 demo 客户端（最快） | `python scripts/mcp_client_demo.py stdio --switch` | 日常自测、CI |
| SSE 服务 + demo 客户端 | `scripts\start_sse.bat 8770 mock` 后 `python scripts/mcp_client_demo.py sse` | 验证网络模式 |
| MCP Inspector | `npx @modelcontextprotocol/inspector` | 可视化看 schema、手动点 |

## 2. 前置

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

`pip install -e .` 用于让 `python -m native_cli_orchestrator.entry` 在任意 cwd 可用。

## 3. 接 Inspector

### stdio

Inspector 选 **STDIO**：

```
Command:   python
Arguments: -m native_cli_orchestrator.entry stdio --config config/config.yaml
```

不要传 `--log-dir ""`（Inspector 会把空字符串当参数转义出错），需要关文件日志就改 `config.yaml` 的 `log_dir: ""`。

### SSE

先起服务：

```bat
scripts\start_sse.bat 8770 mock
```

看到 `SSE 端点：http://127.0.0.1:8770/sse` 后，Inspector 选 **SSE**，填 `http://127.0.0.1:8770/sse`。

局域网：`config.yaml` 里 `server.host: 0.0.0.0`，并建议设 `server.auth_token`。

## 4. 六工具手动验证（Mock）

| 步骤 | 工具 | 入参 | 期望 |
| --- | --- | --- | --- |
| 1 | `get_pool_status` | 空 | `ok=true`，3 个分组，每组只有一个 `active=true` |
| 2 | `submit_task` | `trae_group` + 清单 + `wait=false` | 返回 `task_id`，`state=queued`，`waited=false` |
| 3 | `get_task_result` | `task_id` | 先 `queued/running`，再 `succeeded`（均 `ok=true`） |
| 4 | `get_task_result` | 同上 | `artifacts` 非空，含 `path/size_bytes/sha256` |
| 5 | `switch_account` | `qoder_group` | `switched=true`，`active_label` 变化 |
| 6 | `reload_config` | 空 | `loaded=true`，`accounts_total=7` |
| 7 | `update_upstream` | `check_only=true` | 未配 `upstream.repo` 时返回 `UPSTREAM_UPDATE_FAILED`（属预期） |

## 5. 异常路径（编排侧容错必测）

| 场景 | 做法 | 期望 |
| --- | --- | --- |
| 缺必填 | `submit_task` 只传 `group` | `INVALID_ARGUMENT`，`error.detail.fields` 含 `task_manifest` |
| 非法分组 | `group=foo_group` | `INVALID_ARGUMENT` |
| 任务不存在 | `get_task_result(task_id="t-x")` | `TASK_NOT_FOUND` |
| 账号序号越界 | `switch_account(target_index=99)` | `INVALID_ARGUMENT` |
| 全组耗尽 | 反复 `switch_account` 直到无备用 | `POOL_EXHAUSTED` |
| 任务失败但工具成功 | 用注入故障的 MockRunner 跑 | `ok=true` + `succeeded=false` + `fail_reason` |
| 上游未配置 | `update_upstream` | `UPSTREAM_UPDATE_FAILED` |
| 配置非法 | `reload_config` 指坏文件 | `CONFIG_INVALID` |

## 6. 一键回归

```bat
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy
```

当前：93 passed / ruff 0 / mypy 0。全部在 Mock 或 Fake 执行器下运行，开发机不需要任何 CLI 与环境变量。

## 7. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No module named native_cli_orchestrator` | 未安装或在错误 cwd | `pip install -e .` |
| 端口被占用 | 上次进程未退 | 换端口 `--port 8771` |
| Inspector 连 SSE 立即断开 | 连了 `/messages/` 而不是 `/sse` | 客户端连 `/sse` |
| 所有账号都 `failed` | 之前跑过故障注入，状态被持久化 | 删 `workspace/.pools.state.json` 或 `switch_account` 指定恢复 |
| 任务一直 `queued` | 同组前一个任务未结束（串行） | 等，或查前一个任务的 `stdout.log` |
| `update_upstream` 总失败 | `upstream.repo` 未配置 | 填仓库地址，或先 `--check` |
