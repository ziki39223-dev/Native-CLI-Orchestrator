# CLI 探针使用说明

> 状态：P2 · 目的：把"人工翻三家文档 + 逐个试命令"压缩成一条命令，产出可回填的报告。

## 1. 为什么需要探针

`CliProfile` 里的默认命令是**推测值**：

```python
PROFILES = {
    "trae_group":      CliProfile("trae_group", ("traecli", "mcp-server")),
    "qoder_group":     CliProfile("qoder_group", ("qoder", "mcp", "serve")),
    "workbuddy_group": CliProfile("workbuddy_group", ("workbuddy-mcp",)),
}
```

上游可能：改名、改子命令、改启动参数、需要先登录、或**根本不是说 JSON-RPC 的 MCP server**
（而是"一次性跑完写文件"的纯 CLI）。这些差异不该靠改代码解决，而该由探针测出来、写进配置。

## 2. 用法（工作机）

```bat
python devtools\probe_cli.py
python devtools\probe_cli.py --only traecli     REM 只探一家
python devtools\probe_cli.py --timeout 10       REM 慢机器放宽超时
```

报告写到 `probe-report/probe-<时间戳>.{json,md}`（已 gitignore，不进交付包）。

## 3. 探针做了什么

| 步骤 | 动作 | 目的 |
| --- | --- | --- |
| 1 | 在 PATH 找候选名（含变体：`traecli` / `trae` / `trae-cli` …） | 确认装没装、真实可执行名 |
| 2 | 试 `--version` / `-V` / `-v` / `version` | 记录版本，便于上游更新后对比 |
| 3 | 跑 `--help`，正则提取疑似 MCP 子命令 | 推断启动参数 |
| 4 | 按候选参数逐个拉起 5s，发一个最小 `initialize` 请求 | **判断它是否真说 JSON-RPC** |
| 5 | 汇总成 JSON + Markdown，直接给 `accounts.yml` 片段 | 免手抄 |

安全性：全程只读，不写账号配置、不发业务请求、不改任何文件（除了 `probe-report/`）。

## 4. 怎么读报告

### 4.1 三种典型结论

| 报告显示 | 含义 | 该怎么做 |
| --- | --- | --- |
| MCP 握手 ✅ + 建议 `cli` 有值 | 是标准 MCP server | 把建议片段贴进 `accounts.yml`，直接可用 |
| 找到命令但握手 ⚠️ | 可能不是 MCP，或需要先登录 | 手工跑一次看行为；若是纯 CLI → 设 `long_running: False` |
| 未找到 | 没装 / 不在 PATH | 装 CLI，或把安装目录加进 PATH |

### 4.2 握手 ⚠️ 的三种原因与对策

| 原因 | 现象 | 对策 |
| --- | --- | --- |
| 需要先登录 | 输出里有 login/auth 提示 | 先 `cli login`，再重跑探针 |
| 启动参数不同 | 报 unknown option / unrecognized arguments | 从 `--help` 里挑对的子命令，填进 `cli` |
| 不是 MCP server，是纯 CLI | 进程跑完就退出、输出是普通文本 | 设 `long_running: False`（见 `docs/04` §3.1） |

## 5. 报告里的 `accounts.yml` 片段怎么用

报告会直接给出：

```yaml
groups:
  trae_group:
    cli: ["traecli", "mcp-server"]
  qoder_group:
    cli: ["qoder", "mcp", "serve"]
  workbuddy_group:
    cli: ["workbuddy-mcp"]
```

把它贴进 `config/accounts.yml` 的对应位置（**保留原有的 `accounts` 列表**），然后
`reload_config` 或重启服务。

## 6. 探针本身怎么验证的

开发机上用 `tests/stubs/stub_cli.py` 的 `mcp` 模式造了三个"会说 JSON-RPC 的假 CLI"，
装进临时 PATH 后跑探针，确认：

- 能解析到命令
- 能拿到"版本"
- 能识别握手并给出正确 `cli` 建议
- 不会把输出里的 JSON 片段误当成子命令（早期版本有这个 bug，已用正则修掉）

对应测试：`tests/e2e/test_cli_runner.py` 里的桩集成用例 + 手工验证记录。

## 7. 后续

P2 完成后，探针结论应回填到 `docs/09-deploy-and-packaging.md` §7 的待回填清单。
