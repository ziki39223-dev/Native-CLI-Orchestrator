# 工具契约 v1.1.0（已冻结）

> 状态：**已冻结 v1.1.0**。v1.0.1 新增 `error_code`；v1.1.0 把 `group` 从三选一枚举放开为任意分组名，
> 并新增 `kind` / `api_budget` / token 用量字段（**全部向后兼容**，旧客户端无需改动）。
> 工具名、字段名、错误码只增不改不删。
> 本文与 `src/native_cli_orchestrator/contracts/` 同源，冲突以代码为准并立即回改本文。

## 0. 公共信封

```json
{
  "ok": true,
  "error_code": "OK",
  "message": "",
  "backend": "mock",
  "correlation_id": "c-6ad9826f-3950-000003",
  "elapsed_ms": 3,
  "error": null,
  "data": { }
}
```

| 字段 | 说明 |
| --- | --- |
| `ok` | **判断成功只看这个** |
| `error_code` | 见 §1；成功恒 `OK` |
| `backend` | `mock` / `real`，一眼分辨是否真机 |
| `correlation_id` | 单次调用唯一 ID，日志同 ID 可追溯 |
| `elapsed_ms` | 服务端耗时（不含网络） |
| `error` | 失败时 `{code, message, detail}`，成功为 `null` |
| `data` | 工具专有数据；**失败时恒 `null`** |

## 1. 错误码（15 个，冻结）

| 错误码 | 触发条件 | 编排侧建议 |
| --- | --- | --- |
| `OK` | 成功 | 继续 |
| `INVALID_ARGUMENT` | 入参校验失败（含越界 index）、分组/账号序号非法 | 修参数，不重试 |
| `GROUP_NOT_FOUND` | 分组名不存在 | 修参数 |
| `TASK_NOT_FOUND` | task_id 不存在（含被清理） | 检查 ID |
| `POOL_EXHAUSTED` | 该组全部账号 FAILED/DISABLED | 人工补额度或 `switch_account` 指定恢复 |
| `ACCOUNT_NOT_READY` | 账号凭据缺失 | 配环境变量 |
| `CLI_NOT_FOUND` | CLI 可执行文件找不到 | 装 CLI / 改 `accounts.yml` 的 `cli` |
| `PROCESS_START_FAILED` | 子进程拉起失败 | 看 `cli.log` |
| `TASK_TIMEOUT` | 超过 `timeout_s` 无 `.DONE` | 重试或调大超时 |
| `TASK_FAILED` | 任务失败且不可换号解决 | 看 `fail_reason` |
| `CONFIG_INVALID` | 配置/清单文件非法；有任务在跑时重载配置 | 按 `detail` 修 |
| `UPSTREAM_UPDATE_FAILED` | 上游拉取失败（已回滚） | 查 `upstream.log` |
| `UPSTREAM_INCOMPATIBLE` | 更新后校验不通过（已回滚） | 联系上游 / 暂缓更新 |
| `DEPENDENCY_MISSING` | real 模式非 Windows | 开发机改 mock |
| `INTERNAL_ERROR` | 未预期异常兜底 | 报 bug + `correlation_id` |

## 2. `submit_task`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `group` | 字符串（见 §2.2） | 必填 | 分组名，须匹配 `^[A-Za-z][A-Za-z0-9_-]{0,63}$` |
| `task_manifest` | object | 必填 | 见 §2.1 |
| `timeout_s` | float | 900 | 单次尝试超时，上限 7200 |
| `wait` | bool | `false` | `false` 立即返回 task_id（推荐） |

### 2.1 `task_manifest`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `goal` | string | 是 | 人可读目标 |
| `prompt` | string | 是 | 完整指令，≤ 200000 字符 |
| `acceptance` | string[] | 否 | 验收标准，逐条可核对 |
| `version` | string | 否 | 清单版本，默认 `1.0` |
| `artifacts` | string[] | 否 | 期望产物相对路径 |
| `model` | string | 否 | 指定模型，空则 CLI 默认 |
| `metadata` | object | 否 | 透传元数据 |

出参 `data`：

```json
{
  "task_id": "t-trae-20261022112639-a1b2c3",
  "group": "trae_group",
  "state": "queued",
  "account_index": 0,
  "account_label": "trae-a",
  "workspace": "E:\\...\\workspace\\t-trae-20261022112639-a1b2c3",
  "submitted_at": "2026-10-22T11:26:39.123+08:00",
  "waited": false
}
```

- 异步提交时 `state` 恒为 `queued`，`account_index` 是**预定**账号（执行时可能因故障切换而变）。
- `workspace` 是任务目录绝对路径，编排侧可直接读产物。

### 2.2 分组（v1.1.0 变更）

分组不再限定为三家 CLI，可挂**任意 OpenAI 兼容 API**：

| 分组 | 来源 | 成本 |
| --- | --- | --- |
| `trae_group` / `qoder_group` / `workbuddy_group` | 三家客户端 CLI（内置 `kind: cli`） | 免费额度 |
| 任意自定义名（如 `deepseek_group`、`freellm_group`、`local_group`） | `kind: api` + `base_url` | 自己充值的钱（可设日预算） |

**兼容性说明**：
- 旧调用只传三家里的名字，行为完全不变（CLI 组默认 `kind: cli`）。
- 未知分组名原本在入参校验阶段报 `INVALID_ARGUMENT`，现在会走到调度层报 `GROUP_NOT_FOUND`
  —— 语义不变，只是发现得晚一点。**分组名格式非法**（含空格/符号）仍报 `INVALID_ARGUMENT`。
- 三个内置 CLI 分组必须保留在 `accounts.yml` 里（不用某家时给账号加 `disabled: true`），
  否则配置校验失败 —— 这是为了让"CLI 到底还能不能用"保持可见。

## 3. `get_task_result`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 必填 | — |
| `wait_s` | float | 0 | 最多等多少秒让任务收尾，0=立刻返回 |

出参 `data`：

```json
{
  "task_id": "t-trae-...",
  "group": "trae_group",
  "state": "succeeded",
  "succeeded": true,
  "account_index": 1,
  "account_label": "trae-b",
  "summary": "…",
  "artifacts": [{"path": "artifacts/report.md", "size_bytes": 512, "sha256": "…"}],
  "fail_reason": "",
  "fault_kind": "",
  "error_code": "",
  "exit_code": 0,
  "elapsed_s": 12.3,
  "account_attempts": 2,
  "updated_at": "2026-10-22T11:27:00+08:00",
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "cost_yuan": 0.0,
  "truncated": false
}
```

| 字段 | 何时有意义 | 说明 |
| --- | --- | --- |
| `prompt_tokens` / `completion_tokens` | API 分组 | token 用量；CLI 分组恒为 0 |
| `cost_yuan` | API 分组 | 按 `input/output_price_per_mtok` 估算的花费（元）；未配价格时为 0 |
| `truncated` | API 分组 | 是否因 `max_output_tokens` 被截断（`finish_reason=length`）|

| `state` | `ok` | 含义 | 编排侧动作 |
| --- | --- | --- | --- |
| `queued` | true | 排队中（组内前一个任务未结束） | 继续轮询 |
| `running` | true | 执行中 | 继续轮询 |
| `succeeded` | true | 成功 | 取 `artifacts` |
| `failed` | true | 任务失败（**不是工具失败**） | 看 `fail_reason`/`fault_kind` |
| `cancelled` | true | 已取消 | — |

`fault_kind` 与 `error_code` 的区别（重要）：

| 字段 | 何时有值 | 含义 | 编排侧动作 |
| --- | --- | --- | --- |
| `fault_kind` | 任务真跑过但失败 | 账号侧故障（quota / banned / auth / rate_limit / timeout / crashed） | 该账号已自动下移，通常重试即可 |
| `error_code` | 任务没真正跑起来 | 环境级问题（`CLI_NOT_FOUND` / `DEPENDENCY_MISSING` / `CONFIG_INVALID`） | **告警人工处理**，重试无用 |

环境级错误**不会**把账号标成 FAILED（`ADR-013`），避免"没装 CLI 的机器一次性把账号池打废"。

**关键**：任务失败时 `ok=true`、`succeeded=false`。这是"任务结果" vs "工具调用结果"的区分，
避免编排侧把业务失败当协议错误去重试（会白烧额度）。`account_attempts>1` 说明发生过账号切换。

## 4. `get_pool_status`

| 参数 | 类型 | 默认 |
| --- | --- | --- |
| `group` | 分组名 \| null | null（全部） |

出参 `data`：

```json
{
  "groups": [
    {
      "group": "trae_group",
      "kind": "cli",
      "active_index": 0,
      "queued": 0,
      "running": 0,
      "api_budget": {},
      "accounts": [
        {"index": 0, "label": "trae-a", "state": "active", "active": true, "busy": false,
         "last_fault": null, "last_fault_at": null, "last_used_at": "…", "consecutive_failures": 0, "notes": ""},
        {"index": 1, "label": "trae-b", "state": "dormant", "active": false, "busy": false, …}
      ]
    }
  ],
  "healthy": true,
  "warnings": []
}
```

账号状态机（`state`）：

| 状态 | 含义 |
| --- | --- |
| `dormant` | 备用，**进程未拉起、未登录、无请求** |
| `active` | 本组当前账号，尚未执行任务 |
| `working` | 正在执行任务 |
| `failed` | 故障（额度耗尽/封禁/鉴权/连续失败），需人工恢复 |
| `disabled` | 手动下线 / 配置 disabled，不参与自动调度 |

`kind`：`cli`（子进程 + 免费额度）/ `api`（HTTP + 充值）。

`api_budget`（仅 `kind=api` 有值，CLI 组为 `{}`）：

```json
{
  "date": "2026-10-22",
  "cost_today": 0.002,
  "calls_today": 1,
  "prompt_tokens_today": 1000,
  "completion_tokens_today": 500,
  "daily_budget": 1.0,
  "budget_remaining": 0.998
}
```

不变量：同一 `group` 内 `state∈{active,working}` 的账号数 **≤ 1**（API 组同样适用）。

## 5. `switch_account`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `group` | 分组名 | 必填 | — |
| `target_index` | int \| null | null | 指定序号；null 则切到下一个可用 |
| `reason` | string | `manual` | 仅记录 |

出参 `data`：`{group, previous_index, active_index, active_label, previous_state, switched, notes}`

- 未指定 `target_index` 时，原账号被标为 `disabled`（不参与自动调度）。
- 指定 `target_index` 时，若该账号此前是 `failed`，视为人工修复，恢复为 `active`。
- 没有其它可用账号 → `POOL_EXHAUSTED`。

## 6. `reload_config`

| 参数 | 类型 | 默认 |
| --- | --- | --- |
| `path` | string \| null | null（默认 `config/accounts.yml`） |

出参 `data`：`{loaded, path, groups, accounts_total, config_version}`

- 按 `label` 延续旧状态：改配置不会把正在用的账号下线。
- 有任务正在执行时拒绝重载（`CONFIG_INVALID` + `detail.busy_groups`）。
- **新增/删除 api 分组**同样生效：分派表会同步更新（v1.1.0）。

## 7. `update_upstream`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `check_only` | bool | false | 只检查版本 |
| `force` | bool | false | 忽略版本比较 |

出参 `data`：`{updated, from_version, to_version, backup_path, changed_files, compatible, rolled_back, notes}`

- 失败时返回错误码 `UPSTREAM_UPDATE_FAILED` / `UPSTREAM_INCOMPATIBLE`，且 `detail.rolled_back` 标明是否回滚成功。

## 8. `accounts.yml` 结构（v1.0）

```yaml
config_version: "2025-01-01"
groups:
  trae_group:
    cli: ["traecli", "mcp-server"]   # 可选，覆盖内置默认
    accounts:
      - label: trae-a                # 标签，出参与日志里出现的就是它
        env:
          TRAE_TOKEN: env:TRAE_TOKEN_1   # 只写变量名，真实值在系统环境变量
        args: []                     # 可选，该账号专用参数
        disabled: false
        notes: "主用"
```

API 分组示例（v1.1.0）：

```yaml
groups:
  deepseek_group:
    kind: api
    api:
      protocol: openai                       # 一期只支持 openai 兼容
      base_url: https://api.deepseek.com/v1  # 任意家 / 中转 / 本地
      model: deepseek-chat
      max_output_tokens: 4096
      temperature: 0.3
      request_timeout_s: 300
      daily_budget: 20                       # 日预算（元）；0=不限额
      input_price_per_mtok: 1.0              # 用于估算 cost_yuan
      output_price_per_mtok: 2.0
    accounts:
      - label: ds-a
        env:
          OPENAI_API_KEY: env:DEEPSEEK_KEY_1
```

校验规则：
- `groups` 必须**含三个内置 CLI 分组键**（`trae_group` / `qoder_group` / `workbuddy_group`），
  不用某家时给其账号加 `disabled: true`；API 分组数量与名称自定义；
- 每组 `accounts` 至少 1 个；
- `kind: api` 必须有 `api` 节且**不能**有 `cli`；`kind: cli` 反之；
- `api.base_url` 必须以 `http://` 或 `https://` 开头；
- 带 `token/key/secret/password/cookie` 字样的 env 键，值必须以 `env:` 开头（防明文凭据入库）；
- `_` 开头的键允许（写注释用），其它未知字段报错。

## 8.1 API 组特有行为

| 场景 | 返回 | 是否换 key |
| --- | --- | --- |
| 429 + 余额/额度关键词 | `POOL_EXHAUSTED` + `fault_kind=quota_exhausted` | ✅ 换 |
| 429 + rate limit | `TASK_FAILED` + `fault_kind=rate_limited` | ✅ 换 |
| 401/403 鉴权失效 | `ACCOUNT_NOT_READY` + `fault_kind=auth_invalid` | ✅ 换 |
| 401/403 带封禁关键词 | `TASK_FAILED` + `fault_kind=banned` | ✅ 换 |
| 402 余额不足 | `POOL_EXHAUSTED` + `fault_kind=quota_exhausted` | ✅ 换 |
| 400 上下文超长 | `TASK_FAILED` + `fault_kind=context_too_long` | ❌ **不换**（换 key 无用，应缩 prompt） |
| 404 模型不存在 | `TASK_FAILED` + `fault_kind=model_unavailable` | ❌ **不换**（应改 `model`） |
| 5xx / 网络错误 | `TASK_FAILED` + `fault_kind=crashed` | ✅ 换（可能是该上游节点坏了） |
| 日预算用尽 | `POOL_EXHAUSTED`（不发出请求） | — 需人工提预算或次日再试 |

`fault_kind` 新增两个取值（只增）：

| 值 | 含义 | 编排侧动作 |
| --- | --- | --- |
| `context_too_long` | prompt 超出模型上下文 | 缩短输入 / 换大上下文模型 |
| `model_unavailable` | 模型名不存在或无权限 | 改 `api.model` 或开通权限 |

## 9. 契约稳定性边界

冻结的是**结构**，不冻结：

- Mock 生成的文本内容、`hwnd`、耗时具体值、`correlation_id` 具体值、日志格式；
- `workspace` 里的内部文件内容（除 `manifest.json` / `result.json` 的键）；
- 故障识别关键词表（可随上游文案调整，不影响错误码语义）。

## 10. 下版本待增（只增不改）

- [ ] `get_task_result` 增加 `progress`（stdout 尾部摘要），便于 DSH 展示"还在写"；
- [ ] `get_pool_status` 增加 `quota_hint`（从 CLI 输出解析的剩余额度，可能为空）；
- [ ] `submit_task` 增加 `idempotency_key`，避免 DSH 重试导致重复任务。
