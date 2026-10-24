# 接任意家 API（OpenAI 兼容）

> 状态：P2 · 本文是"怎么配"的操作手册；设计取舍见 `docs/07` ADR-014/015/016/017，分层图见 `06-api-runners.md`。

## 1. 一句话

除三家 CLI 的免费额度外，本项目还能挂**任意 OpenAI 兼容 API** ——
DSH 自己充值就能用，也支持 FreeLLMAPI 这类免费网关、OpenRouter 这类聚合、以及本地 vLLM/Ollama。

## 2. 最小配置（三步）

### 步骤 1：把 key 放进系统环境变量

```bat
setx DEEPSEEK_KEY_1 "sk-xxxxxxxx"
REM 录完要重开终端
```

**不要**写进 `accounts.yml` —— 配置文件里有校验器，明文 key 会直接报错。

### 步骤 2：在 `config/accounts.yml` 里加一个 api 分组

```yaml
groups:
  # 三家 CLI 组保持不动（必须有，不用某家时给账号加 disabled: true）
  trae_group: { accounts: [ { label: trae-a } ] }
  qoder_group: { accounts: [ { label: qoder-a } ] }
  workbuddy_group: { accounts: [ { label: wb-a } ] }

  # 新增 API 组
  deepseek_group:
    kind: api
    api:
      protocol: openai
      base_url: https://api.deepseek.com/v1
      model: deepseek-chat
      max_output_tokens: 4096
      temperature: 0.3
      request_timeout_s: 300
      daily_budget: 20              # 日预算（元），超了当天拒单
      input_price_per_mtok: 1.0     # 用于估算 cost_yuan
      output_price_per_mtok: 2.0
    accounts:
      - label: ds-a
        env:
          OPENAI_API_KEY: env:DEEPSEEK_KEY_1
      - label: ds-b                 # 备用 key，额度耗尽自动切
        env:
          OPENAI_API_KEY: env:DEEPSEEK_KEY_2
```

### 步骤 3：重载配置

```
reload_config
```

或重启服务。然后 `get_pool_status` 应能看到新分组（`kind: api`）。

## 3. 供应商 base_url 速查

| 供应商 | base_url | 常用 model | 备注 |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | 官方 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` / `deepseek-reasoner` | 性价比高、中文好 |
| Moonshot(Kimi) | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` | 长上下文 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` | |
| 阿里 Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 用"兼容模式"端点 |
| OpenRouter | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` | 模型名带 `作者/` 前缀 |
| FreeLLMAPI 类网关 | 网关给的地址 | 网关支持的模型名 | 建议 `daily_budget: 0` |
| Ollama（本地） | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` | key 随便填非空值 |
| vLLM / SGLang | `http://127.0.0.1:8000/v1` | 加载的模型名 | 同上 |

**判断某家能不能用**：只要它文档里写了"OpenAI 兼容"或"兼容 OpenAI 协议"，填上 `base_url` 就能用。

## 4. 预算护栏怎么用

| `daily_budget` | 行为 | 适用 |
| --- | --- | --- |
| `0`（默认） | 不限额，只统计 token 与花费 | 免费网关、本地模型 |
| `> 0` | 当日累计花费达到上限后拒单（`POOL_EXHAUSTED`） | 付费供应商，**建议必填** |

看点：

- 账本落盘在 `workspace/.api_budget.json`，**重启不清零**。
- 跨天自动重置（按 `date` 字段）。
- 拒单发生在**发请求之前**，不会"先花钱再报错"。
- `get_pool_status` 的 `api_budget` 字段能看到当日花费与剩余。

价格怎么填：供应商官网的价格表通常按"每百万 token"报价，直接填数字即可（人民币元）。
不知道价格就先填 0，只统计 token；跑几天看日志里的 `prompt/completion` 用量再决定。

## 5. 计量与成本可见性

每次 API 调用后：

- `get_task_result.data` 带 `prompt_tokens` / `completion_tokens` / `cost_yuan` / `truncated`
- 任务目录 `result.json` 里有完整记录
- `logs/cli.log` 里有 `API 用量 group=... prompt=... cost=... 今日累计=...`

## 6. 多 key 轮换

与 CLI 组完全一样：

1. 同一分组里按顺序排 key；
2. 当前 key 遇到**可切换故障**（配额耗尽/封禁/鉴权失效/限流/5xx）→ 自动标 `FAILED` → 切下一个；
3. 单任务最多试 `runner.max_account_attempts` 个 key（默认 3）。

**不会切**的情况（重要）：
- `context_too_long`（prompt 太长）→ 换 key 无用，应缩短输入；
- `model_unavailable`（模型名错）→ 改 `api.model`；
- `INVALID_ARGUMENT` / 配置错误 → 改配置。

## 7. 与 CLI 组混用

```yaml
# Trae 出初稿（免费额度），DeepSeek 再润色（花钱但便宜）
trae_group:     { kind: cli, accounts: [...] }
deepseek_group: { kind: api, accounts: [...] }
```

DSH 侧按分组名路由即可，两组可并行（各持独立锁），各自遵守单实例。

## 8. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ACCOUNT_NOT_READY`：环境变量 X 未设置 | key 没配到环境变量 | `setx` 后重开终端 |
| `INVALID_ARGUMENT`：env 看起来是凭据 | 把明文 key 写进了配置文件 | 改成 `env:变量名` |
| `POOL_EXHAUSTED`：预算已用尽 | 当日超过 `daily_budget` | 提预算，或删/改 `workspace/.api_budget.json` |
| `TASK_FAILED` + `context_too_long` | prompt 超模型上下文 | 缩短输入、分批，或换大上下文模型 |
| `TASK_FAILED` + `model_unavailable` | 模型名不存在或无权限 | 核对 `api.model` 与账号权限 |
| `TASK_FAILED` + `crashed`（5xx） | 上游节点故障 | 会自动换 key；若全组 5xx 说明是上游问题 |
| HTTP 400 但看不出原因 | 某家字段支持不一 | 只用了 `model/messages/max_tokens/temperature`，一般不会；查 `inbox/request.json` |
| 连不上本地 Ollama | 端口/地址错 | 确认 `ollama serve` 在跑，`base_url` 是 `http://127.0.0.1:11434/v1` |

## 9. 已验证 / 未验证

| 项 | 状态 |
| --- | --- |
| OpenAI 兼容请求体构造、响应解析 | ✅ 单测（29 项） |
| 10 种 HTTP 错误 → FaultKind 映射 | ✅ 单测（参数化） |
| 预算护栏（耗尽拒单、跨重启保留、0=不限额） | ✅ 单测 |
| key 轮换（429 配额 → 切下一个 key 成功） | ✅ 集成测试（MockTransport） |
| API 组与 CLI 组共存、单实例不变量 | ✅ 单测 |
| **真实供应商联网调用** | ⬜ 需在有 key 的机器上跑一次 |
| **各家真实错误体措辞** | ⬜ 关键词表可能需按实际报错补充 |

最后两项只能在有 key 的环境验证。若遇到没识别出来的错误，把 `fault_kind` 与原始响应贴到
`observability/faults.py` 或 `runners/api_client.py` 的关键词表里即可（不动契约）。
