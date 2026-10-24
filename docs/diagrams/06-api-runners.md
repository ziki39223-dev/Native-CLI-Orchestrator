# 分层图 L5：API 分组（OpenAI 兼容）

> 状态：P2 · 本轮只落 L5（API 组怎么接进已有的账号池与调度）。上一层 L4 见 `05-upstream-rollback.md`。

## L5-1 分层位置：与 CLI 组平级，不侵入调度器

```
Scheduler（完全不变：组锁 / 轮换 / 冷却 / 预算外的一切）
   │ 持有一个 Runner 接口
   ▼
CompositeRunner  ──按 group.kind 分派──┐
   │                                   │
   ├─ kind=cli ──► CliRunner            │  子进程 + 文件协议（三家免费额度）
   └─ kind=api ──► ApiRunner            │  HTTP + chat/completions（自己充值）
                        │
                        ├─ BudgetLedger（.api_budget.json，按天）
                        └─ api_client（OpenAI 兼容 + 错误翻译）
```

关键点：**调度器一行没改**。所有"API 特有能力"都收在执行器内部，
因为只有执行器知道 token 用量与 HTTP 语义。

## L5-2 一次 API 任务的生命

```
Scheduler._execute
  ├─ pool.acquire()                     ← 与 CLI 组完全相同的选号逻辑
  ├─ slot.config.api_key(env)           ← 从 env: 引用取 key（缺则标 AUTH_INVALID 换号）
  ├─ runner.run(context)                ← CompositeRunner → ApiRunner
  │    ├─ _check_budget()               ← 日预算用尽 ⇒ POOL_EXHAUSTED（不发请求）
  │    ├─ build_chat_body()             ← model / messages / max_tokens / temperature
  │    ├─ 写 inbox/request.json         ← 与 CLI 协议目录结构一致（不含 key）
  │    ├─ call_chat()                   ← httpx2 POST {base_url}/chat/completions
  │    ├─ 成功 ⇒ 解析 usage，记账本，写 artifacts/response.md + result.json
  │    └─ 失败 ⇒ ApiError → classify → RunResult(fault, retry_with_other_account)
  └─ 与 CLI 组共用：mark_fault / deactivate_current / 冷却 / 重试上限
```

## L5-3 错误翻译表（本层最核心的逻辑）

```
HTTP 状态 + 错误体关键词          →  FaultKind              换 key?
────────────────────────────────────────────────────────────────
429 + quota/billing/额度/余额     →  quota_exhausted         ✅
429 + rate limit                  →  rate_limited            ✅
401/403 + banned/suspended/风控   →  banned                  ✅
401/403 其它                      →  auth_invalid            ✅
402                               →  quota_exhausted         ✅
400 + context length              →  context_too_long        ❌ 缩 prompt
404 + model_not_found             →  model_unavailable       ❌ 改配置
408 / 504                         →  timeout                 ✅
5xx                               →  crashed                 ✅ 可能该节点坏了
其它 4xx                          →  None                    ❌
```

判据不是"失败了"，而是**"这个 key 是不是不能用了"**。
把 `context_too_long` 当成 key 故障去轮换，会把整组好 key 一个个标成 FAILED 而额度一分没用完。

## L5-4 预算护栏

```
_api_budget.json
{
  "deepseek_group": {
    "date": "2026-10-22",
    "cost": 0.002,
    "calls": 1,
    "prompt_tokens": 1000,
    "completion_tokens": 500
  }
}
```

- **按天**：跨天自动开新桶（`date` 不等于今天即重置）。
- **落盘**：服务重启不清零 —— 否则护栏形同虚设。
- **只统计不算钱**：`input/output_price_per_mtok` 都为 0 时 `cost=0`，
  适合免费网关（不想算账）与"先观察再定价"的场景。
- **拒单而非报错**：超预算返回 `POOL_EXHAUSTED`（组不可用），语义上等同"这家额度用完了"。

## L5-5 与 CLI 组的三条一致性

| 不变量 | CLI 组 | API 组 |
| --- | --- | --- |
| 组内串行 | 持组锁 | 同样持组锁 |
| 组内单实例 | 只有 1 个进程 | 只有 1 个 key 在用（其余休眠＝不发请求） |
| 跨组并行 | 各组独立锁 | 同样独立锁 |

区别只在"休眠"的物理含义：CLI 是进程不拉起，API 是不发请求。

## L5-6 已被测试覆盖

| 行为 | 测试 |
| --- | --- |
| 请求体构造（model/max_tokens/temperature/messages） | `test_success_writes_artifact_and_usage` |
| usage 解析与花费估算 | 同上 + `test_estimate_cost` |
| manifest.model 覆盖分组默认 | `test_manifest_model_overrides_group_default` |
| 10 种错误分类 | `test_classify_http_error`（参数化） |
| 配额/鉴权 ⇒ 换 key | `test_quota_error_switches_account` / `test_auth_error_switches_account` |
| 上下文超长/模型不存在 ⇒ 不换 | `test_context_too_long_does_not_retry` 等 |
| 超时 ⇒ TIMEOUT 且可换 | `test_timeout_maps_to_timeout_fault` |
| 预算耗尽 ⇒ 拒单 | `test_daily_budget_blocks_after_exhaustion` |
| 预算跨重启保留 | `test_budget_persists_across_instances` |
| 预算 0 ⇒ 不限额 | `test_no_budget_means_unlimited` |
| CLI 与 API 组共存 | `test_cli_and_api_groups_coexist` |
| API 组同样单实例 | `test_api_group_single_instance_invariant` |
| 热重载新增 API 组 | `test_reload_config_can_add_api_group` |
| 出参带 token 与花费 | `test_api_group_reports_tokens_and_cost` |

## L5-7 实际接入清单

| 供应商 | base_url | 备注 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | 官方 |
| DeepSeek | `https://api.deepseek.com/v1` | 便宜，中文好 |
| Moonshot(Kimi) | `https://api.moonshot.cn/v1` | 长上下文 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | |
| 阿里 Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 兼容模式 |
| OpenRouter | `https://openrouter.ai/api/v1` | 聚合，模型名带前缀 |
| FreeLLMAPI 类网关 | 网关给出的地址 | 免费额度，建议 `daily_budget: 0` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | key 随便填个非空值 |
| 本地 vLLM / SGLang | `http://127.0.0.1:8000/v1` | 同上 |

## 下一层

五层已全部落地。后续如需扩展，建议按同样粒度新增（如 L6 API 流式输出、L7 多供应商预算汇总）。
