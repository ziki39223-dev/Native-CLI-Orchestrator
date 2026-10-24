# 目录分区与文件归属

> 状态：P1 · 目录已全部落位，文件已实现。

## 1. 顶层树

```
Native-CLI-Orchestrator/
├─ README.md
├─ pyproject.toml · requirements*.txt      依赖与打包元数据
├─ .gitignore · .editorconfig
├─ src/native_cli_orchestrator/
│  ├─ entry/          sse.py / stdio.py / cli.py / args.py  ← 进程入口
│  ├─ server/         app.py（MCPServer 装配、六工具注册、生命周期）
│  ├─ tools/          service.py（工具 → 调度器 → 信封 → 错误映射）
│  ├─ pools/          manager.py（GroupPool/PoolManager）· scheduler.py（Scheduler）
│  ├─ runners/        base.py（Runner 接口）· mock.py · cli.py
│  ├─ tasks/          workspace.py（manifest/inbox/.DONE/产物/脱敏落盘）
│  ├─ upstream/       updater.py（拉取/备份/校验/回滚）
│  ├─ contracts/      constants.py · errors.py · tools.py  ← 已冻结
│  ├─ config/         settings.py · accounts.py · loader.py
│  ├─ common/         ids.py · text.py（脱敏）· retry.py
│  └─ observability/  logging.py（三本日志）· timing.py · faults.py（故障识别）
├─ config/            config.yaml · accounts.yml（模板，含禁用账号示例）
├─ scripts/           start_sse.bat · start_stdio.bat · upstream-update.bat
│                     mcp_client_demo.py · upstream_update.py
├─ tests/             unit（68 项）· contract（20 项）· e2e（4 项）
├─ devtools/          （预留：真机诊断脚本）
├─ workspace/         运行期任务目录（gitignore）
├─ backups/           上游更新备份（gitignore）
└─ docs/ · docs/diagrams/
```

## 2. 各层职责与禁忌

| 目录 | 职责 | 允许依赖 | 禁止 |
| --- | --- | --- | --- |
| `entry/` | 解析参数、加载配置、选传输、启停 | server、config | 业务逻辑 |
| `server/` | 造 Server、注册六工具、生命周期 | tools、contracts、observability | 直接碰 pools/runners |
| `tools/` | 入参校验、调调度器、出参封装、错误映射、日志 | pools、upstream、contracts | 出现 subprocess / 文件协议细节 |
| `pools/` | 账号状态机、轮换、任务表、组锁 | runners、tasks、config | 出现 MCP 相关代码 |
| `runners/` | 执行一次任务尝试（Mock / 真实子进程） | tasks、contracts、observability | 修改账号池状态（由调度器负责） |
| `tasks/` | 工作目录协议、产物收集、脱敏落盘 | contracts、common | 决策"该不该切账号" |
| `upstream/` | 备份/同步/校验/回滚 | config、contracts | 触碰账号状态 |
| `contracts/` | 模型、错误码、常量 | 仅 stdlib + pydantic | import 任何本项目上层 |
| `observability/` | 三本日志、计时、故障识别 | common | 业务判断 |

## 3. 关键文件职责速查

| 文件 | 一句话 |
| --- | --- |
| `pools/manager.py` | 单实例激活状态机的唯一实现，`next_available` 不回绕到自身 |
| `pools/scheduler.py` | 组锁 + 重试循环 + 任务表 + 持久化；失败→标记→换号的全流程 |
| `runners/cli.py` | `CREATE_NO_WINDOW` 起进程、流式脱敏、`taskkill /T /F` 收尾 |
| `runners/mock.py` | 场景表（quota/banned/auth/rate_limit/timeout/crash）模拟真实故障分布 |
| `tasks/workspace.py` | 协议文件读写；`.DONE`/`.FAILED` 互斥；产物 sha256 |
| `observability/faults.py` | 关键词表 + 退出码兜底；`is_env_error` 区分"环境问题不换号" |
| `upstream/updater.py` | 先备份→同步→校验→失败回滚；备份轮转 |

## 4. 命名与卫生

1. 文件 `snake_case`，一文件一职责，超 ~300 行拆分。
2. `workspace/`、`backups/`、`logs/`、`.pools.state.json` 一律 gitignore，不进交付包。
3. 一次性脚本、探测数据只写 `.ai-workspace-runtime/tmp/`。
4. `config/accounts.yml` 里**不得**出现明文凭据（有校验器强制）。
