# 从这里开始（3 步跑起来）

> 目标是：**拿到这个文件夹，配一下就能用**。不需要改代码，不需要懂架构。

---

## 第 0 步：确认前提（1 分钟）

| 需要 | 检查方法 | 没有怎么办 |
| --- | --- | --- |
| Python 3.12+ | 命令行执行 `python --version` | 装 [Python 3.12+](https://www.python.org/downloads/)，安装时勾选 **Add python.exe to PATH** |
| 要用的 CLI 或 API key | `traecli --version` / 有 `sk-...` 的 key | 装 CLI，或直接用 API 分组（见第 2 步方式 B） |

> 三条路都能用：**只接 API**（不需要任何 CLI）、**只接 CLI**（不需要花钱）、**两者混用**。

---

## 第 1 步：一键安装（双击即可）

```bat
scripts\setup.bat
```

它会自动：建虚拟环境 → 装依赖 → 补齐缺失配置 → 跑环境自检并告诉你还差什么。

装完再看一眼自检结果：

```bat
scripts\doctor.bat
```

- 全是 `[OK]` → 直接进第 2 步
- 有 `[ERROR]` → 每条都带 `→ 怎么修`，照着改，再跑一次 `scripts\doctor.bat`

---

## 第 2 步：配置账号（只有这一步需要动脑）

### 方式 A：用三家 CLI 的免费额度

**A-1 设凭据**（真实 token 只写在这里，不进配置文件）

```bat
copy scripts\set_keys.example.bat scripts\set_keys.bat
notepad scripts\set_keys.bat      REM 把里面的"在这里填..."换成真实 token
scripts\set_keys.bat
```

**A-2 改账号池**：打开 `config\accounts.yml`，确认每个账号的 `env:` 变量名与上一步的
`setx` 变量名一致（默认模板已经对好了，一般不用改）。

### 方式 B：接任意 OpenAI 兼容 API（DSH 自己充值也能用）

**B-1 设 key**

```bat
setx DEEPSEEK_KEY_1 "sk-你的key"
```

**B-2 在 `config\accounts.yml` 末尾取消注释并填好**：

```yaml
  deepseek_group:
    kind: api
    api:
      base_url: https://api.deepseek.com/v1   # 换成你用的家的地址
      model: deepseek-chat
      daily_budget: 20                        # 日预算（元），超了当天拒单
      input_price_per_mtok: 1.0               # 用来估算花费
      output_price_per_mtok: 2.0
    accounts:
      - label: ds-a
        env:
          OPENAI_API_KEY: env:DEEPSEEK_KEY_1
```

各家 `base_url` 速查表见 [docs/15-api-providers.md](docs/15-api-providers.md)。

> **改完环境变量必须重开终端**，否则读不到。

---

## 第 3 步：切真机模式并启动

改 `config\config.yaml` 第一行：

```yaml
mode: real          # 原来是 mock，改成 real
```

然后启动：

```bat
scripts\start_sse.bat
```

看到这行就成功了：

```
SSE 端点：http://127.0.0.1:8770/sse
```

**把这个地址填进你的上层工作台（DSH / Cherry Studio 等）的 MCP 配置里，类型选 SSE。**

---

## 验证是否真的能用

开一个**新**终端（服务要一直开着）：

```bat
.venv\Scripts\python scripts\mcp_client_demo.py sse --switch
```

预期输出末尾是 `[结果] 全部通过`。

若想只跑一家：

```bat
.venv\Scripts\python scripts\mcp_client_demo.py sse --group trae_group
```

---

## 常见卡点（按出现频率排序）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `scripts\doctor.bat` 报 `找不到命令 traecli` | CLI 没装 / 不在 PATH | 装 CLI；或先改用 API 分组；或跑 `python devtools\probe_cli.py` 查真实命令 |
| 自检报"环境变量 XXX 未设置" | 忘了重开终端 | 关掉所有终端重开；`setx` 只对新终端生效 |
| `accounts.yml 校验失败` | 分组名/字段写错 | 自检会把出错字段路径列出来，照着改 |
| 端口 8770 被占用 | 上次进程没退 | `scripts\start_sse.bat 8771` 换端口 |
| 任务一直 `queued` | 同组前一个任务没结束 | 正常（组内串行），等一会儿 |
| 全部账号 `failed` | 额度真用完了 | 补账号后 `reload_config`；或 `switch_account` 指定恢复 |
| 工作台连不上 SSE | 跨机访问 | `config.yaml` 改 `host: 0.0.0.0` + 设 `auth_token`，放行防火墙 |

---

## 三条需要知道的规则

1. **同一分组同一时刻只有一个账号在跑**（其余休眠，不发请求）。这是为了降低风控与保证额度不被抢。
2. **不同分组可以并行**：Trae 在跑，Qoder 和 API 组照样能干别的活。
3. **额度耗尽会自动换下一个账号**，不需要人工干预；全部耗完才会失败并告警。

---

## 想看更深的东西

| 想知道 | 看 |
| --- | --- |
| 六个工具怎么调 | [docs/06-tool-contract.md](docs/06-tool-contract.md) |
| 各家 API 的 base_url 与价格 | [docs/15-api-providers.md](docs/15-api-providers.md) |
| 三家 CLI 的真实命令怎么查 | [docs/14-cli-probe.md](docs/14-cli-probe.md) |
| 接 DSH 的完整步骤 | [docs/11-integration-dsh.md](docs/11-integration-dsh.md) |
| 为什么这么设计 | [docs/07-decision-log.md](docs/07-decision-log.md) |
| 出问题了怎么查 | [docs/12-troubleshooting.md](docs/12-troubleshooting.md) |
